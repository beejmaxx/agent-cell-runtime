FROM python:3.12-slim
RUN mkdir /rootfs-probe && chown 65532:65532 /rootfs-probe
COPY fake_agent.py probes.py /
ENV PYTHONDONTWRITEBYTECODE=1
USER 65532
ENTRYPOINT ["python", "/fake_agent.py"]
