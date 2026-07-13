# PlayMCP in KC 배포용 Dockerfile
# 저장소 루트에 이 파일이 있어야 Git 소스 빌드가 가능합니다.
FROM python:3.12-slim

WORKDIR /app

# 의존성 먼저 설치 (레이어 캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 서버 코드 복사
COPY server.py .

# server.py가 PORT 환경변수를 읽도록 되어 있음 (기본 8000)
ENV PORT=8000
EXPOSE 8000

CMD ["python", "server.py"]
