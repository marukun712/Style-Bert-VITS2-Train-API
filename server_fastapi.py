"""
API server for TTS
TODO: server_editor.pyと統合する?
"""

import argparse
import io
import os
import subprocess
import sys
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

import GPUtil
import psutil
import torch
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from scipy.io import wavfile

from config import get_config
from gradio_tabs.train import get_path, preprocess_all
from style_bert_vits2.constants import (
    DEFAULT_ASSIST_TEXT_WEIGHT,
    DEFAULT_LENGTH,
    DEFAULT_LINE_SPLIT,
    DEFAULT_NOISE,
    DEFAULT_NOISEW,
    DEFAULT_SDP_RATIO,
    DEFAULT_SPLIT_INTERVAL,
    DEFAULT_STYLE,
    DEFAULT_STYLE_WEIGHT,
    Languages,
)
from style_bert_vits2.logging import logger
from style_bert_vits2.nlp import bert_models
from style_bert_vits2.nlp.japanese import pyopenjtalk_worker
from style_bert_vits2.nlp.japanese import pyopenjtalk_worker as pyopenjtalk
from style_bert_vits2.nlp.japanese.user_dict import update_dict
from style_bert_vits2.tts_model import TTSModel, TTSModelHolder


config = get_config()
ln = config.server_config.language


# pyopenjtalk_worker を起動
## pyopenjtalk_worker は TCP ソケットサーバーのため、ここで起動する
pyopenjtalk.initialize_worker()

# dict_data/ 以下の辞書データを pyopenjtalk に適用
update_dict()

# 事前に BERT モデル/トークナイザーをロードしておく
## ここでロードしなくても必要になった際に自動ロードされるが、時間がかかるため事前にロードしておいた方が体験が良い
bert_models.load_model(Languages.JP)
bert_models.load_tokenizer(Languages.JP)
bert_models.load_model(Languages.EN)
bert_models.load_tokenizer(Languages.EN)
bert_models.load_model(Languages.ZH)
bert_models.load_tokenizer(Languages.ZH)


def raise_validation_error(msg: str, param: str):
    logger.warning(f"Validation error: {msg}")
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=[dict(type="invalid_params", msg=msg, loc=["query", param])],
    )


class AudioResponse(Response):
    media_type = "audio/wav"


loaded_models: list[TTSModel] = []


def load_models(model_holder: TTSModelHolder):
    global loaded_models
    loaded_models = []
    for model_name, model_paths in model_holder.model_files_dict.items():
        model = TTSModel(
            model_path=model_paths[0],
            config_path=model_holder.root_dir / model_name / "config.json",
            style_vec_path=model_holder.root_dir / model_name / "style_vectors.npy",
            device=model_holder.device,
        )
        # 起動時に全てのモデルを読み込むのは時間がかかりメモリを食うのでやめる
        # model.load()
        loaded_models.append(model)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu", action="store_true", help="Use CPU instead of GPU")
    parser.add_argument(
        "--dir", "-d", type=str, help="Model directory", default=config.assets_root
    )
    args = parser.parse_args()

    if args.cpu:
        device = "cpu"
    else:
        device = "cuda" if torch.cuda.is_available() else "cpu"

    model_dir = Path(args.dir)
    model_holder = TTSModelHolder(model_dir, device)
    if len(model_holder.model_names) == 0:
        logger.error(f"Models not found in {model_dir}.")
        sys.exit(1)

    logger.info("Loading models...")
    load_models(model_holder)

    limit = config.server_config.limit
    if limit < 1:
        limit = None
    else:
        logger.info(
            f"The maximum length of the text is {limit}. If you want to change it, modify config.yml. Set limit to -1 to remove the limit."
        )
    app = FastAPI()
    allow_origins = config.server_config.origins
    if allow_origins:
        logger.warning(
            f"CORS allow_origins={config.server_config.origins}. If you don't want, modify config.yml"
        )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server_config.origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    # app.logger = logger
    # ↑効いていなさそう。loggerをどうやって上書きするかはよく分からなかった。

    @app.api_route("/voice", methods=["GET", "POST"], response_class=AudioResponse)
    async def voice(
        request: Request,
        text: str = Query(..., min_length=1, max_length=limit, description="セリフ"),
        encoding: str = Query(None, description="textをURLデコードする(ex, `utf-8`)"),
        model_name: str = Query(
            None,
            description="モデル名(model_idより優先)。model_assets内のディレクトリ名を指定",
        ),
        model_id: int = Query(
            0, description="モデルID。`GET /models/info`のkeyの値を指定ください"
        ),
        speaker_name: str = Query(
            None,
            description="話者名(speaker_idより優先)。esd.listの2列目の文字列を指定",
        ),
        speaker_id: int = Query(
            0, description="話者ID。model_assets>[model]>config.json内のspk2idを確認"
        ),
        sdp_ratio: float = Query(
            DEFAULT_SDP_RATIO,
            description="SDP(Stochastic Duration Predictor)/DP混合比。比率が高くなるほどトーンのばらつきが大きくなる",
        ),
        noise: float = Query(
            DEFAULT_NOISE,
            description="サンプルノイズの割合。大きくするほどランダム性が高まる",
        ),
        noisew: float = Query(
            DEFAULT_NOISEW,
            description="SDPノイズ。大きくするほど発音の間隔にばらつきが出やすくなる",
        ),
        length: float = Query(
            DEFAULT_LENGTH,
            description="話速。基準は1で大きくするほど音声は長くなり読み上げが遅まる",
        ),
        language: Languages = Query(ln, description="textの言語"),
        auto_split: bool = Query(DEFAULT_LINE_SPLIT, description="改行で分けて生成"),
        split_interval: float = Query(
            DEFAULT_SPLIT_INTERVAL, description="分けた場合に挟む無音の長さ（秒）"
        ),
        assist_text: Optional[str] = Query(
            None,
            description="このテキストの読み上げと似た声音・感情になりやすくなる。ただし抑揚やテンポ等が犠牲になる傾向がある",
        ),
        assist_text_weight: float = Query(
            DEFAULT_ASSIST_TEXT_WEIGHT, description="assist_textの強さ"
        ),
        style: Optional[str] = Query(DEFAULT_STYLE, description="スタイル"),
        style_weight: float = Query(DEFAULT_STYLE_WEIGHT, description="スタイルの強さ"),
        reference_audio_path: Optional[str] = Query(
            None, description="スタイルを音声ファイルで行う"
        ),
    ):
        """Infer text to speech(テキストから感情付き音声を生成する)"""
        logger.info(
            f"{request.client.host}:{request.client.port}/voice  {unquote(str(request.query_params))}"
        )
        if request.method == "GET":
            logger.warning(
                "The GET method is not recommended for this endpoint due to various restrictions. Please use the POST method."
            )
        if model_id >= len(
            model_holder.model_names
        ):  # /models/refresh があるためQuery(le)で表現不可
            raise_validation_error(f"model_id={model_id} not found", "model_id")

        if model_name:
            # load_models() の 処理内容が i の正当性を担保していることに注意
            model_ids = [
                i
                for i, x in enumerate(model_holder.models_info)
                if x.name == model_name
            ]
            if not model_ids:
                raise_validation_error(
                    f"model_name={model_name} not found", "model_name"
                )
            # 今の実装ではディレクトリ名が重複することは無いはずだが...
            if len(model_ids) > 1:
                raise_validation_error(
                    f"model_name={model_name} is ambiguous", "model_name"
                )
            model_id = model_ids[0]

        model = loaded_models[model_id]
        if speaker_name is None:
            if speaker_id not in model.id2spk.keys():
                raise_validation_error(
                    f"speaker_id={speaker_id} not found", "speaker_id"
                )
        else:
            if speaker_name not in model.spk2id.keys():
                raise_validation_error(
                    f"speaker_name={speaker_name} not found", "speaker_name"
                )
            speaker_id = model.spk2id[speaker_name]
        if style not in model.style2id.keys():
            raise_validation_error(f"style={style} not found", "style")
        assert style is not None
        if encoding is not None:
            text = unquote(text, encoding=encoding)
        sr, audio = model.infer(
            text=text,
            language=language,
            speaker_id=speaker_id,
            reference_audio_path=reference_audio_path,
            sdp_ratio=sdp_ratio,
            noise=noise,
            noise_w=noisew,
            length=length,
            line_split=auto_split,
            split_interval=split_interval,
            assist_text=assist_text,
            assist_text_weight=assist_text_weight,
            use_assist_text=bool(assist_text),
            style=style,
            style_weight=style_weight,
        )
        logger.success("Audio data generated and sent successfully")
        with BytesIO() as wavContent:
            wavfile.write(wavContent, sr, audio)
            return Response(content=wavContent.getvalue(), media_type="audio/wav")

    @app.post("/g2p")
    def g2p(text: str):
        return g2kata_tone(normalize_text(text))

    @app.get("/models/info")
    def get_loaded_models_info():
        """ロードされたモデル情報の取得"""

        result: dict[str, dict[str, Any]] = dict()
        for model_id, model in enumerate(loaded_models):
            result[str(model_id)] = {
                "config_path": model.config_path,
                "model_path": model.model_path,
                "device": model.device,
                "spk2id": model.spk2id,
                "id2spk": model.id2spk,
                "style2id": model.style2id,
            }
        return result

    @app.post("/models/refresh")
    def refresh():
        """モデルをパスに追加/削除した際などに読み込ませる"""
        model_holder.refresh()
        load_models(model_holder)
        return get_loaded_models_info()

    @app.get("/status")
    def get_status():
        """実行環境のステータスを取得"""
        cpu_percent = psutil.cpu_percent(interval=1)
        memory_info = psutil.virtual_memory()
        memory_total = memory_info.total
        memory_available = memory_info.available
        memory_used = memory_info.used
        memory_percent = memory_info.percent
        gpuInfo = []
        devices = ["cpu"]
        for i in range(torch.cuda.device_count()):
            devices.append(f"cuda:{i}")
        gpus = GPUtil.getGPUs()
        for gpu in gpus:
            gpuInfo.append(
                {
                    "gpu_id": gpu.id,
                    "gpu_load": gpu.load,
                    "gpu_memory": {
                        "total": gpu.memoryTotal,
                        "used": gpu.memoryUsed,
                        "free": gpu.memoryFree,
                    },
                }
            )
        return {
            "devices": devices,
            "cpu_percent": cpu_percent,
            "memory_total": memory_total,
            "memory_available": memory_available,
            "memory_used": memory_used,
            "memory_percent": memory_percent,
            "gpu": gpuInfo,
        }

    @app.post("/train")
    async def train(request: Request):
        form = await request.form()
        name = form["name"]
        transcript = form["transcript"]
        file = form["file"]

        assert not isinstance(file, str)
        file_content = await file.read()

        audio_file = io.BytesIO(file_content)

        id: str = str(uuid.uuid4())

        os.makedirs(f"Data/{id}/raw", exist_ok=True)

        with open(f"Data/{id}/raw/voice.wav", "xb") as f:
            f.write(audio_file.getbuffer())

        with open(f"Data/{id}/esd.list", "x") as f:
            f.write(f"voice.wav|{name}|JP|{transcript}")

        try:
            # 上でつけたフォルダの名前`Data/{model_name}/`
            model_name = id

            # JP-Extra （日本語特化版）を使うかどうか。日本語の能力が向上する代わりに英語と中国語は使えなくなります。
            use_jp_extra = True

            # 学習のバッチサイズ。VRAMのはみ出具合に応じて調整してください。
            batch_size = 6

            # 学習のエポック数（データセットを合計何周するか）。
            # 100で多すぎるほどかもしれませんが、もっと多くやると質が上がるのかもしれません。
            epochs = 15

            # 保存頻度。何ステップごとにモデルを保存するか。分からなければデフォルトのままで。
            save_every_steps = 3

            # 音声ファイルの音量を正規化するかどうか
            normalize = False

            # 読みのエラーが出た場合にどうするか。
            # "raise"ならテキスト前処理が終わったら中断、"skip"なら読めない行は学習に使わない、"use"なら無理やり使う
            yomi_error = "skip"

            pyopenjtalk_worker.initialize_worker()

            preprocess_all(
                model_name=model_name,
                batch_size=batch_size,
                epochs=epochs,
                save_every_steps=save_every_steps,
                num_processes=2,
                normalize=normalize,
                trim=False,
                freeze_EN_bert=False,
                freeze_JP_bert=False,
                freeze_ZH_bert=False,
                freeze_style=False,
                freeze_decoder=False,
                use_jp_extra=use_jp_extra,
                val_per_lang=0,
                log_interval=200,
                yomi_error=yomi_error,
            )

            # 上でつけたモデル名を入力。学習を途中からする場合はきちんとモデルが保存されているフォルダ名を入力。
            model_name = id

            paths = get_path(model_name)
            dataset_path = str(paths.dataset_path)
            config_path = str(paths.config_path)
            assets_root = "model_assets/"

            with open("default_config.yml", "r", encoding="utf-8") as f:
                yml_data = yaml.safe_load(f)
            yml_data["model_name"] = model_name
            with open("config.yml", "w", encoding="utf-8") as f:
                yaml.dump(yml_data, f, allow_unicode=True)

            command = [
                "python",
                "train_ms_jp_extra.py",
                "--config",
                config_path,
                "--model",
                dataset_path,
                "--assets_root",
                assets_root,
            ]

            subprocess.run(command, capture_output=False)

            return JSONResponse(content={"id": id})
        except:
            return JSONResponse(content={"error": "err!"})

    @app.get("/tools/get_audio", response_class=AudioResponse)
    def get_audio(
        request: Request, path: str = Query(..., description="local wav path")
    ):
        """wavデータを取得する"""
        logger.info(
            f"{request.client.host}:{request.client.port}/tools/get_audio  {unquote(str(request.query_params))}"
        )
        if not os.path.isfile(path):
            raise_validation_error(f"path={path} not found", "path")
        if not path.lower().endswith(".wav"):
            raise_validation_error(f"wav file not found in {path}", "path")
        return FileResponse(path=path, media_type="audio/wav")

    logger.info(f"server listen: http://127.0.0.1:{config.server_config.port}")
    logger.info(f"API docs: http://127.0.0.1:{config.server_config.port}/docs")
    logger.info(
        f"Input text length limit: {limit}. You can change it in server.limit in config.yml"
    )
    uvicorn.run(
        app, port=config.server_config.port, host="0.0.0.0", log_level="warning"
    )
