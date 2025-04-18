FROM pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime

RUN pip install nvidia-cublas-cu12 nvidia-cudnn-cu12

ENV CUDA_VISIBLE_DEVICES 0
ENV MKL_SERVICE_FORCE_INTEL 1
ENV LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/opt/conda/lib/python3.10/site-packages/nvidia/cublas/lib:/opt/conda/lib/python3.10/site-packages/nvidia/cudnn/lib:/opt/conda/lib/python3.10/site-packages/torch/lib

WORKDIR /work

COPY requirements.txt ./
RUN pip install -r requirements.txt
