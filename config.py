"""수집 설정값."""
import os
from pathlib import Path

KEYWORDS = [
    "buldak challenge",
    "spicy noodle challenge",
    "shin ramyun",
    "korean ramen recipe",
]

# 검색 조건 (북미 시청자 기준)
REGION_CODE = "US"
RELEVANCE_LANGUAGE = "en"

# 수집 기준 (runs.criteria에 기록되어 대시보드가 이 기준의 데이터만 골라 씀)
#   v1_date_latest       : 최신순, 기간 제한 없음 (2026-11-26 1차 수집)
#   v2_relevance_3to30d  : 관련도순, 수집 시점 기준 3~30일 전 업로드
CRITERIA = "v2_relevance_3to30d"
SEARCH_ORDER = "relevance"
PUBLISHED_MIN_DAYS_AGO = 3
PUBLISHED_MAX_DAYS_AGO = 30

# 지표 계산: 업로드 후 경과 일수 하한
MIN_ELAPSED_DAYS = 1

# 수집 한도
MAX_SHORTS_PER_KEYWORD = 50   # 키워드당 쇼츠 최대 개수
SEARCH_PAGE_SIZE = 50         # search.list 1페이지 크기 (API 최대 50)
MAX_SEARCH_PAGES = 2          # 쇼츠가 부족할 때 키워드당 최대 페이지 수

# 쇼츠 판정 기준 (유튜브 공식 기준)
# - 정사각형 또는 세로(높이 >= 너비)이고
# - 2024-10-15 이후 업로드면 180초 이하, 그 이전 업로드면 60초 이하
# - 가로 영상은 길이와 상관없이 일반 영상
SHORT_RULE_CHANGE_AT = "2024-10-15T00:00:00Z"  # UTC 기준
SHORT_MAX_SECONDS = 180       # 2024-10-15 이후 업로드
SHORT_MAX_SECONDS_OLD = 60    # 2024-10-15 이전 업로드
EMBED_MAX_WIDTH = 480         # player.embedWidth/Height를 받기 위한 기준 너비

# API 유닛 비용
UNIT_COST = {"search": 100, "videos": 1, "channels": 1, "comments": 1}

# DB 위치 (SHORTS_DB_PATH 환경변수로 바꿀 수 있음: 테스트·배포용)
DB_PATH = Path(os.environ.get("SHORTS_DB_PATH") or Path(__file__).parent / "data" / "shorts.db")

# 자동 수집 (대시보드를 열 때 확인)
AUTO_COLLECT = os.environ.get("AUTO_COLLECT", "1") != "0"   # AUTO_COLLECT=0 이면 끔
REFRESH_HOURS = 24            # 마지막 성공 수집 후 이 시간이 지나면 새로 검색
RETRY_AFTER_FAILURE_MINUTES = 60   # 수집 실패 후 재시도 대기

# 데이터 보관 기간 (YouTube API 정책의 30일 갱신·삭제 기준)
RETENTION_DAYS = 30

# 콘텐츠 포맷 분류 (OpenAI) — 정의·우선순위는 CLAUDE.md 참고
FORMATS = ["챌린지", "레시피·조리", "먹방·ASMR·맛평가", "기타"]   # v2: 코미디·상황극 제외 (CLAUDE.md)
FORMAT_CONFIDENCES = ["높음", "중간", "낮음"]
FORMAT_MODEL = "gpt-6-luna"           # 2026-11-26 확인: 현행 모델 중 최저가
FORMAT_PRICE_PER_1M = {"input": 0.10, "cached_input": 0.01, "output": 0.50}   # USD, 표준 요금
FORMAT_PROMPT_VERSION = "v2"          # 포맷 목록·프롬프트를 바꾸면 올림 → 전체 재분류 (v1: 5개 포맷)
FORMAT_DESCRIPTION_CHARS = 1000       # 설명은 앞부분만 보냄 (비용 절약)
FORMAT_MIN_SAMPLE = 5                 # 이 수 미만이면 "표본 부족"
FORMAT_OTHER_REVIEW_SHARE = 0.15      # 기타가 이 비율을 넘으면 목록 재검토
# 댓글 반응 분석 (YouTube commentThreads + OpenAI)
COMMENTS_PER_VIDEO = 20               # 관련도 상위 N개, 답글 제외
COMMENT_TEXT_CHARS = 1000             # 분류에 보내는 댓글 최대 길이
COMMENT_MIN_PER_VIDEO = 5             # 영상별 비율 계산에 필요한 최소 분석 댓글 수 (미만은 제외하고 개수 표시)
SENTIMENTS = ["긍정", "부정", "중립"]
PURCHASE_INTENTS = ["있음", "없음"]
COMMENT_MODEL = "gpt-6-luna"
COMMENT_REASONING_EFFORT = "low"      # none은 응답의 약 41%에 JSON 뒤 잡음이 붙어 실패 (2026-11-26 확인)
COMMENT_MAX_OUTPUT_TOKENS = 300
COMMENT_PROMPT_VERSION = "v2"         # v1: reasoning none (부분 실패, 사용 안 함) / v2: reasoning low
COMMENT_WORKERS = 8                   # 댓글은 하나씩 독립 분류하되 동시에 여러 요청 (시간 단축)
COMMENT_CAVEAT = ("댓글은 관련도 상위 20개만 분석, 댓글 작성자는 시청자 일부라 전체 반응을 대표하지 않음, "
                  "긍정 댓글이 실제 구매로 이어지는지는 알 수 없음")

FORMAT_ACCURACY_SAMPLE = 50           # format_eval.py 표본 크기 (정확도 검증은 생략하기로 결정 — CLAUDE.md)
FORMAT_VALIDATION_NOTE = ("사람이 50개를 직접 라벨링한 결과 약 15개(30%)는 한 영상에 여러 포맷이 섞여 "
                          "단일 포맷으로 정하기 어려웠음. 단일 포맷 분류의 정확도를 정의하기 어려워 "
                          "LLM 분류 결과를 그대로 사용")
