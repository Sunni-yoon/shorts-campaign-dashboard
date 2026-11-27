"""수집 설정값."""
import os
from pathlib import Path

# v3: 키워드 buldak 하나로 모아 포맷별로 비교 (v1·v2는 4개 키워드 비교)
KEYWORDS = ["buldak"]

# 검색 조건 (북미 시청자 기준)
REGION_CODE = "US"
RELEVANCE_LANGUAGE = "en"

# 수집 기준 (runs.criteria에 기록되어 대시보드가 이 기준의 데이터만 골라 씀)
#   v1_date_latest       : 최신순, 기간 제한 없음 (2026-11-26 1차 수집)
#   v2_relevance_3to30d  : 관련도순, 수집 시점 기준 3~30일 전 업로드 (키워드 4개)
#   v3_buldak_3to30d     : v2와 같은 조건, 키워드 buldak 하나·쇼츠 최대 200개
CRITERIA = "v3_buldak_3to30d"
SEARCH_ORDER = "relevance"
PUBLISHED_MIN_DAYS_AGO = 3
PUBLISHED_MAX_DAYS_AGO = 30

# 지표 계산: 업로드 후 경과 일수 하한
MIN_ELAPSED_DAYS = 1
# 구독자 대비 조회수는 이 구독자 수 이상 채널의 영상만 계산 (작은 채널은 분모가 작아 값이 극단적으로 커짐)
VPS_MIN_SUBSCRIBERS = 1000

# 수집 한도
MAX_SHORTS_PER_KEYWORD = 200  # 키워드당 쇼츠 최대 개수
SEARCH_PAGE_SIZE = 50         # search.list 1페이지 크기 (API 최대 50)
MAX_SEARCH_PAGES = 6          # 쇼츠가 부족할 때 키워드당 최대 페이지 수 (200개 ÷ 50 + 여유)

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

# 대시보드: 섹션 제목 아래 "관찰 · ..." 한 줄 표시 여부 (문장 생성은 observations.py에 그대로 있음)
SHOW_OBSERVATIONS = False

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
FORMAT_PROMPT_VERSION = "v3.1"        # 포맷 목록·프롬프트를 바꾸면 올림 → 전체 재분류
#   v1: 5개 포맷 / v2: 4개 / v3: 종이·점토 공예·모형은 기타 / v3.1: v3 규칙 그대로, 출력 순서 근거 → 포맷 → 확신도
FORMAT_TRIALS = 3                     # 영상마다 이 횟수만큼 분류해 다수결 (2회 이상 나온 포맷, 모두 다르면 기타)
FORMAT_TIE_FALLBACK = "기타"           # 모든 회차 결과가 다를 때의 최종 포맷
FORMAT_DESCRIPTION_CHARS = 1000       # 설명은 앞부분만 보냄 (비용 절약)
FORMAT_MIN_SAMPLE = 5                 # 이 수 미만이면 "표본 부족"
FORMAT_OTHER_REVIEW_SHARE = 0.15      # 기타가 이 비율을 넘으면 목록 재검토
# 그래프 포맷 색·순서 (모든 그래프 공통, 기타는 그래프에서 제외). dataviz 기본 팔레트 1~3번 슬롯 — 색맹 검사 통과
FORMAT_COLORS = {"챌린지": "#257de0", "레시피·조리": "#f47230", "먹방·ASMR·맛평가": "#00b894"}
# 댓글 감성 색 (포맷 색과 겹치지 않게). 긍정·부정 짝 색맹 검사 통과, 중립은 회색
SENTIMENT_COLORS = {"긍정": "#6a4ce2", "중립": "#c8c8c8", "부정": "#ec4647"}
FORMAT_EXCLUDED_FROM_COMPARISON = "기타"   # 내용이 섞인 묶음이라 포맷 비교 표에서 '비교 제외'로 표시 (값은 그대로 보여 줌)
# 기타 구성 (참고용 개수, 분류 결과는 바꾸지 않음). 기타 영상에만 위에서부터 적용, 먼저 걸린 그룹 하나만
#   field: text = 제목·설명·해시태그 / title = 제목만 / low_confidence = LLM 확신도 '낮음'
OTHER_GROUPS = [
    ("종이 DIY", "text", r"(?<!rock)paper"),             # rockpaperscissors(가위바위보) 제외
    ("랭킹·모음", "title", r"\brank"),
    ("밈", "text", r"birth\s?(?:day\s)?month|\bmemes?\b"),
    ("판단 불가 (제목·설명에 정보 부족)", "low_confidence", None),
]
OTHER_GROUP_REST = "그 외"
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
FORMAT_ACCURACY_SAMPLE = 50           # format_eval.py 표본 크기 (정확도 검증은 생략하기로 결정 — CLAUDE.md)

# 대시보드 "해석 주의" 문구 (한 곳에서 관리 — 화면에는 한 줄씩 "- " 목록으로 그대로 표시)
CAVEATS = [
    "표본: 키워드 검색의 관련도 상위 영상이라 무작위 표본이 아님. 성과가 좋은 영상 쪽으로 치우쳐 있을 수 있음.",
    "제공되지 않는 데이터: 공유 수·시청 시간·비용·전환은 YouTube Data API가 제공하지 않아 포함하지 않음. "
    "광고 조회수와 자연 조회수도 구분할 수 없음.",
    "구독자 수: YouTube가 반올림해 제공하는 값(유효숫자 3자리).",
    "포맷별 집계: 영상마다 포맷 하나만 배정되어 중복 집계 없음. 미분류 영상은 포맷별 표에서 빠지고 '전체' 행에만 포함.",
    "크리에이터 표: 영상 수는 이번 수집에 잡힌 이 채널의 불닭 쇼츠 수 (채널 전체 영상 수가 아님)",
    "댓글 범위: 영상별 관련도 상위 댓글 20개만 분석함. 댓글 작성자는 시청자 일부라 전체 반응을 대표하지 "
    "않으며, 구매의향 댓글이 실제 구매로 이어졌는지는 알 수 없음.",
    "댓글 감성의 대상: 감성은 댓글 전체 분위기로 판단하며 대상(제품·출연자·연출)을 구분하지 않음.",
]
CAVEAT_COMMENT_TARGET = next(c for c in CAVEATS if c.startswith("댓글 감성의 대상"))   # 반응 섹션에도 같은 문장 표시
