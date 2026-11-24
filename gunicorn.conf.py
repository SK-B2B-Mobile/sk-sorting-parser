# ★ 2026-11-24 — Render 서버(gunicorn) 설정. 이 파일은 gunicorn 이 시작할 때 자동으로 읽음(같은 폴더의 gunicorn.conf.py).
#   큰 PDF(60쪽 이상)는 Render 무료 서버(느린 CPU)에서 읽는 데 30초 넘게 걸릴 수 있는데,
#   gunicorn 기본 시간 제한(30초)에 걸리면 읽는 도중 강제 종료 → 화면엔 "Failed to fetch".
#   → 시간 제한을 10분으로 늘림. (Render 'Start Command'에 --timeout 을 따로 적어 두었다면 그 값이 우선)
timeout = 600
graceful_timeout = 30
workers = 1
