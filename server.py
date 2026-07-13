"""
💘 리얼 데이트 시뮬레이터 MCP 서버
==================================
실제 지도 데이터 기반 데이트 코스 시뮬레이션 게임.

[이 서버가 하는 일 vs LLM이 하는 일]
- 서버(이 코드): 게임의 "심판" 역할.
  세션·호감도·라운드 상태를 기억하고, 카카오맵에서 장소를 검색하고,
  점수 규칙을 집행합니다.
- 클라이언트 LLM(PlayMCP의 AI): 게임의 "배우" 역할.
  상대방 페르소나를 연기하고, 사용자의 선택이 취향에 맞는지 판정합니다.

왜 이렇게 나눴냐면 — "파스타를 골랐는데 상대가 초밥파인지" 판단하려면
자연어 이해가 필요해서 일반 코드로는 불가능하기 때문이에요.
그래서 판정은 LLM에게 맡기고, 서버는 그 판정값을 규칙 안에서만
반영되도록 관리(클램핑, 합산, 은닉)합니다.

프레임워크: 독립 패키지 fastmcp (https://gofastmcp.com)

실행:
    KAKAO_REST_API_KEY=<카카오 REST API 키> python server.py
    (키가 없으면 서버가 시작되지 않습니다 — 목업 폴백 없음)

Transport: HTTP (PlayMCP 등 원격 MCP 호스팅용)
"""

# from __future__ import annotations:
# 타입 표기(예: str | None)를 구버전 파이썬에서도 문제없이 쓰게 해주는 관용구.
# 파일 맨 위에 두는 것이 규칙이라 항상 첫 import로 옵니다.
from __future__ import annotations

# ── 표준 라이브러리 (파이썬에 기본 내장된 도구들) ──
import os        # 환경변수(API 키, 포트 번호) 읽기용
import random    # 돌발 이벤트를 "확률적으로" 발생시키기 위한 난수 생성
import uuid      # 세션마다 겹치지 않는 고유 ID를 만들기 위한 도구
from dataclasses import dataclass, field  # "데이터 묶음 클래스"를 간편하게 정의하는 도구
from typing import Any                     # "어떤 타입이든 올 수 있음"을 뜻하는 타입 표기

# ── 외부 라이브러리 (pip로 설치한 것들) ──
import httpx                          # 카카오 API에 HTTP 요청을 보내는 라이브러리
from fastmcp import FastMCP           # MCP 서버를 쉽게 만들어주는 프레임워크
from fastmcp.exceptions import ToolError  # "LLM에게 그대로 전달되는" 에러 타입

# ===========================================================================
# [기능 1] 서버 생성
# ===========================================================================
# FastMCP 객체 하나가 곧 "MCP 서버"입니다.
# 아래에서 @mcp.tool 데코레이터를 붙인 함수들이 이 서버의 tool로 등록됩니다.
#
# instructions는 이 서버에 접속한 LLM이 가장 먼저 읽는 "게임 전체 규칙서"예요.
# 어떤 순서로 tool을 호출해야 하는지, 점수를 숨겨야 한다는 핵심 규칙을
# 여기서 못박아 둡니다.

mcp = FastMCP(
    "sumroute-date-rehearsal",  # 서버 이름 (클라이언트에 표시됨)
    instructions=(
        "썸루트 MCP: 실제 장소 검색을 활용해 데이트 코스를 미리 리허설하는 서비스입니다. "
        "start_date를 호출하기 전 반드시 4가지 필수값을 모두 확인하세요: 상대 성별, 상대 성격/취향, 관계 단계, 만남 장소. "
        "특히 상대 성격/취향(partner_personality_and_taste)이 없으면 식당·활동 선택의 점수 판단이 약해지므로 반드시 먼저 물어보세요. "
        "반드시 start_date로 세션을 만든 뒤, 각 라운드마다 search_nearby → 선택지 제시 → "
        "make_choice 순서로 진행하세요. 라운드는 식사, 액티비티, 카페·소품샵, 마무리 순서입니다. "
        "서버는 사용자의 선택 코스, 숨겨진 호감도, 선택별 효과를 기록합니다. "
        "LLM은 데이트 상대를 자연스럽게 연기하되, 평소에는 호감도 수치를 직접 말하지 말고 "
        "표정·말투·텐션으로만 표현하세요. 돌발 이벤트는 세션 안에서 중복 없이 낮은 확률로 발생하며, "
        "이벤트 자체는 점수를 자동 변경하지 않고 사용자의 대응을 make_choice로 기록할 때만 점수에 반영됩니다. "
        "선물 이벤트는 '줄까 말까'가 아니라 조용히 챙기기·직접 표현하기·다음 만남에 기억해두기·같이 고르기·실용템 제안·손편지처럼 표현 방식으로 선택지를 주세요. "
        "모든 라운드가 끝나면 get_result를 호출해 사용자가 선택한 전체 코스, 성적표, 그리고 '이렇게 했으면 점수가 더 올랐을 플랜B'를 정리하세요. "
        "예상 비용은 이 서버가 검증하지 않으므로 구체적인 금액처럼 단정하지 마세요."
    ),
)

# 환경변수에서 카카오 API 키를 읽습니다.
# os.environ.get("이름", "기본값") → 환경변수가 없으면 기본값("")을 돌려줍니다.
# 코드에 API 키를 직접 적지 않는 이유: 코드를 GitHub 등에 올릴 때 키가
# 유출되는 사고를 막기 위해서예요. (실무에서 아주 중요한 습관!)
KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY", "")
KAKAO_LOCAL_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"

# 같은 파라미터로 반복 검색할 때 Kakao API 호출을 줄이기 위한 단순 메모리 캐시입니다.
# 같은 서버 프로세스가 살아있는 동안만 유지됩니다. 실서비스에서는 Redis 캐시로 교체하는 것을 권장합니다.
KAKAO_SEARCH_CACHE: dict[tuple[tuple[str, str], ...], list[dict[str, Any]]] = {}
MAX_CACHE_ENTRIES = int(os.environ.get("MAX_CACHE_ENTRIES", "500"))

# 검색 범위 제한: 데이트 코스가 너무 멀어지지 않도록 기준 만남 장소 주변만 검색합니다.
# 지하철 한 정거장 내외를 대략 1.2km로 보고, 모든 장소 검색에 이 반경을 적용합니다.
NEARBY_RADIUS_M = int(os.environ.get("NEARBY_RADIUS_M", "1200"))
# 같은 장소가 반복 노출되지 않도록 후보를 넓게 가져온 뒤 섞어서 보여줍니다.
# API 호출량을 줄이기 위해 기본은 키워드당 1페이지만 가져오고,
# 후보가 부족할 때만 반경 확장으로 보완합니다.
SEARCH_PAGE_SIZE = int(os.environ.get("SEARCH_PAGE_SIZE", "15"))
SEARCH_MAX_PAGES = int(os.environ.get("SEARCH_MAX_PAGES", "1"))
SEARCH_RETURN_SIZE = int(os.environ.get("SEARCH_RETURN_SIZE", "5"))
MIN_FRESH_RESULTS = int(os.environ.get("MIN_FRESH_RESULTS", "5"))
# 너무 가까운 곳만 반복될 때 단계적으로 반경을 넓힙니다. 기본 1.2km → 1.6km → 2.0km
RADIUS_EXPANSION_STEPS = [
    NEARBY_RADIUS_M,
    int(os.environ.get("NEARBY_RADIUS_MID", "1600")),
    int(os.environ.get("NEARBY_RADIUS_MAX", "2000")),
]

# 목업 폴백이 없으므로, 키가 없으면 서버를 시작조차 하지 않습니다 (fail-fast).
# 배포 후에야 "검색이 이상하네?"로 뒤늦게 발견하는 것보다,
# 애초에 Active 상태가 안 되는 쪽이 문제를 훨씬 빨리 드러냅니다.
if not KAKAO_REST_API_KEY:
    raise RuntimeError(
        "KAKAO_REST_API_KEY 환경변수가 설정되지 않았습니다. "
        "로컬 실행 시: `KAKAO_REST_API_KEY=<키> python server.py` / "
        "KC 배포 시: '시크릿'에 KAKAO_REST_API_KEY를 등록하세요."
    )

# ===========================================================================
# [기능 2] 게임 규칙 상수 정의
# ===========================================================================
# "상수"란 게임 도중 바뀌지 않는 고정 설정값입니다.
# 파이썬에서는 대문자 이름으로 쓰는 것이 관례예요.
# 밸런스 조정(점수, 확률 등)이 필요하면 이 구역만 고치면 됩니다.

# 라운드 진행표: 게임은 이 리스트의 순서대로 딱 4라운드 진행됩니다.
# 각 라운드는 딕셔너리(키-값 묶음)로, LLM에게 줄 안내문을 담고 있어요.
ROUNDS: list[dict[str, Any]] = [
    {
        "name": "식사",
        "search_hint": (
            "식사 라운드입니다. MCP가 내부 키워드 풀에서 다양한 식사 카테고리를 자동 검색합니다. "
            "LLM은 반환된 장소만 사용하고, 메뉴 다양성·대화 편의성·다음 코스 이동성을 기준으로 설명하세요."
        ),
        "description": (
            "첫 라운드. 식사는 데이트의 첫 분위기를 정하는 단계입니다. "
            "반환된 장소 중 3~4개를 골라, 부담 없는지, 대화하기 편한지, "
            "다음 코스로 이동하기 쉬운지를 비교하세요. "
            "첫 데이트/썸이면 너무 비싸거나 먹기 불편한 메뉴는 주의해서 설명하세요."
        ),
    },
    {
        "name": "액티비티",
        "search_hint": (
            "액티비티 라운드입니다. MCP가 내부 키워드 풀에서 체험형·구경형·놀이형 활동 후보를 자동 검색합니다. "
            "LLM은 반환된 장소만 사용하고, 과하지 않은지·대화가 자연스러운지·이동 부담이 적은지를 기준으로 설명하세요."
        ),
        "description": (
            "두 번째 라운드. 액티비티는 데이트의 분위기를 본격적으로 만드는 단계입니다. "
            "반환된 장소 중 3~4개를 골라, 부담 없는 구경형인지, 대화가 이어지는 체험형인지, "
            "가볍게 웃을 수 있는 놀이형인지 비교하세요. "
            "관계가 어색하면 짧고 자연스러운 활동을, 친한 사이라면 몰입형 활동도 후보로 설명할 수 있습니다."
        ),
    },
    {
        "name": "카페·소품샵",
        "search_hint": (
            "카페·소품샵 라운드입니다. MCP가 내부 키워드 풀에서 쉬는 장소와 가벼운 구경 장소를 자동 검색합니다. "
            "LLM은 반환된 장소만 사용하고, 대화하기 좋은지·취향을 볼 수 있는지·선물 이벤트가 자연스러운지를 기준으로 설명하세요."
        ),
        "description": (
            "세 번째 라운드. 이 구간은 쉬면서 대화하고 상대 취향을 자연스럽게 확인하는 단계입니다. "
            "카페만 반복하지 말고, 반환된 후보 안에서 휴식형 장소와 구경형 장소를 섞어 제시하세요. "
            "작은 선물 이벤트가 발생하면 '안 사준다'가 아니라 표현 방식 중심으로 선택지를 구성하세요. "
            "예: 조용히 챙기기, 직접 표현하기, 다음 만남에 기억해두기, 같이 고르기, 실용템 제안, 짧은 메모 더하기."
        ),
    },
    {
        "name": "마무리",
        "search_hint": (
            "마무리 라운드입니다. MCP가 내부 키워드 풀에서 끝맺음 장소와 귀가 동선 후보를 자동 검색합니다. "
            "LLM은 반환된 장소만 사용하고, 배려·여운·귀가 편의·다음 만남 가능성을 기준으로 설명하세요."
        ),
        "description": (
            "마지막 라운드. 단순히 산책이나 야경만 고르는 단계가 아니라, 데이트를 어떻게 끝낼지 선택하는 단계입니다. "
            "반환된 후보를 바탕으로 역까지 배웅하기, 짧게 걷기, 포토부스, 따뜻한 음료 포장, "
            "귀가 동선 확인처럼 장소와 행동을 함께 제안하세요. "
            "과한 고백보다 자연스러운 배려와 여운을 중심으로 평가하세요."
        ),
    },
]

# 라운드별 자동 검색 키워드 풀.
# 기존에는 LLM이 query를 잘 만들어야 해서 description 의존도가 컸습니다.
# 이제 search_nearby는 query가 없어도 현재 라운드와 meeting_place를 바탕으로
# 서버가 직접 검색 키워드를 골라 Kakao Local API를 호출합니다.
ROUND_KEYWORD_POOLS: dict[str, list[str]] = {
    "식사": [
        # 너무 특정 메뉴(김밥/떡볶이 등)로 쏠리면 같은 유형만 반복되므로,
        # 기본 자동 검색은 넓은 식사 카테고리와 데이트에 무난한 메뉴를 우선 사용합니다.
        "한식", "양식", "일식", "중식", "브런치", "캐주얼 다이닝", "조용한 밥집", "샤브샤브",
        "쌀국수", "라멘", "돈카츠", "포케", "파스타", "피자", "버거", "샌드위치",
        "태국음식", "베트남음식", "인도커리", "멕시칸", "오므라이스", "초밥", "딤섬",
        "백반", "샐러드", "비건", "고기집", "이자카야 식사",
        # 아래는 후보가 반복될 때 쓸 수 있는 보조 키워드입니다. 기본 검색 다양화 로직에서 섞이되,
        # 최종 선택지는 source_keyword가 한쪽으로 몰리지 않도록 다시 분산됩니다.
        "분식", "우동", "리조또",
    ],
    "액티비티": [
        "팝업스토어", "독립서점", "서점", "셀프사진관", "네컷사진", "사진관", "전시", "소극장",
        "영화관", "오락실", "아케이드", "볼링장", "만화카페", "보드게임카페", "방탈출", "공방",
        "도자기 공방", "향수 공방", "가죽 공방", "캔들 공방", "원데이클래스", "LP바", "재즈바",
        "편집샵", "플리마켓", "실내 스포츠", "VR",
    ],
    "카페·소품샵": [
        "조용한 카페", "감성 카페", "디저트 카페", "베이커리", "티룸", "북카페", "로스터리",
        "푸딩", "젤라또", "아이스크림", "케이크", "소품샵", "문구점", "편집샵", "라이프스타일샵",
        "플라워샵", "빈티지샵", "레코드샵", "향수샵", "잡화점", "캐릭터샵", "선물가게",
    ],
    "마무리": [
        "산책로", "공원", "하천길", "야경", "전망대", "포토스팟", "포토부스", "지하철역",
        "버스정류장", "택시 승강장", "베이커리", "디저트 포장", "편의점", "조용한 골목", "광장",
    ],
}

# search_nearby가 한 번에 몇 개의 자동 키워드를 섞어 검색할지.
# 너무 많으면 API 호출이 과하고, 너무 적으면 후보 다양성이 약해집니다.
AUTO_KEYWORD_COUNT = int(os.environ.get("AUTO_KEYWORD_COUNT", "3"))

# 후보 점수화에 쓰는 거리 기준. 한 정거장 내외 동선은 유지하되, 너무 가까운 곳만 반복되지 않게 합니다.
IDEAL_DISTANCE_M = int(os.environ.get("IDEAL_DISTANCE_M", "1200"))

# 점수 변동이 이 값(±8) 이상으로 클 때만 💖/💔 이펙트를 노출합니다.
# 기획서 3장의 "평소엔 점수를 숨기고, 큰 변동일 때만 보여주기" 규칙이에요.
REVEAL_THRESHOLD = 8

# 특수 이벤트별 고정 보너스 점수표. (기획서의 점수 기준표를 코드로 옮긴 것)
# LLM이 make_choice를 호출할 때 event_bonus="surprise_gift"처럼
# 문자열 이름으로 지정하면, 서버가 이 표에서 점수를 찾아 더해줍니다.
EVENT_BONUS_BASE: dict[str, int] = {
    # 선물 이벤트는 “사준다/안 사준다”가 아니라 “어떤 방식으로 마음을 표현하느냐”가 핵심입니다.
    # 보너스는 일부러 작게 둡니다. 최종 점수는 장소/동선 선택(taste_match_delta)이 중심이고,
    # 선물·돌발 이벤트는 보조 변수로만 작동해야 S/A가 너무 쉽게 나오지 않습니다.
    "gift_quiet": 1,       # 몰래/자연스럽게 챙겨서 건네기
    "gift_direct": 1,      # 대놓고 “사줄게”라고 표현하기
    "gift_later": 1,       # 기억해뒀다가 다음 만남/기념일에 주기
    "gift_together": 1,    # 같이 고르자고 제안하기
    "gift_practical": 1,   # 상대가 실제로 쓸 만한 실용템으로 챙기기
    "gift_note": 1,        # 짧은 메모/손편지/한마디를 더해 의미를 만들기
    "crisis_handled": 3,   # 돌발 이벤트를 배려 있게 처리
    "crisis_mishandled": -5,  # 돌발 이벤트 대응이 아쉬움
    "repeat_date": -6,     # 관계 단계에 비해 너무 반복적인 데이트
    "none": 0,             # 특수 이벤트 없음
}

# 돌발 랜덤 이벤트 목록. 라운드가 넘어갈 때 아래 확률로 하나가 뽑힙니다.
RANDOM_EVENTS: list[str] = [
    "갑자기 비가 쏟아지기 시작한다. (우산은 하나뿐이다…!)",
    "가려던 곳 앞에 웨이팅이 1시간이라고 한다.",
    "상대방이 새 신발 때문에 발이 아파 보인다.",
    "상대방의 옛 친구를 우연히 마주쳤다. 분위기가 미묘해진다.",
    "상대방 휴대폰 배터리가 3% 남았다며 초조해한다.",
]
RANDOM_EVENT_CHANCE = 0.25  # 25% 확률: 너무 자주 터지면 피로하므로 낮춤

# ===========================================================================
# [기능 3] 세션 = 게임 한 판의 상태 저장
# ===========================================================================
# "세션"은 게임 한 판의 모든 상태(누구랑, 어디서, 호감도 몇 점, 몇 라운드째)를
# 담는 상자입니다. LLM은 대화가 길어지면 상태를 잊거나 왜곡할 수 있어서,
# 점수처럼 정확해야 하는 값은 반드시 서버가 기억해야 해요.
#
# @dataclass는 "데이터를 담는 클래스"를 자동으로 만들어주는 문법입니다.
# 원래라면 __init__ 같은 코드를 직접 써야 하지만, 필드 이름과 타입만
# 나열하면 파이썬이 알아서 만들어줘요.


@dataclass
class Session:
    # ── 게임 시작 시 사용자가 입력하는 값들 ──
    session_id: str          # 이 게임 판의 고유 번호표
    partner_gender: str      # 상대방 성별
    meeting_place: str       # 만나는 장소 (장소 검색의 기준점)
    persona: str             # 상대방 성격/취향/입맛 (자유 텍스트)
    relationship_stage: str  # 만난 기간 (썸 / 1~3개월 / 1년차 / 장기연애)
    center_x: str | None = None  # 만남 장소 기준 경도. search_nearby에서 최초 1회 자동 해석
    center_y: str | None = None  # 만남 장소 기준 위도. search_nearby에서 최초 1회 자동 해석
    center_place_name: str | None = None  # 좌표 기준으로 사용된 실제 장소명
    shown_place_keys: set[str] = field(default_factory=set)  # 이번 세션에서 이미 선택지로 보여준 장소 식별자
    search_count_by_round: dict[str, int] = field(default_factory=dict)  # 라운드별 검색 호출 횟수
    used_search_keywords: set[str] = field(default_factory=set)  # 이번 세션에서 이미 사용한 자동 검색 키워드
    used_random_events: set[str] = field(default_factory=set)  # 한 시나리오 안에서 이미 나온 돌발 이벤트

    # ── 게임 진행 중 계속 바뀌는 값들 (= 기본값이 있는 필드) ──
    affection: int = 50      # 호감도. 50에서 시작해 0~100 사이를 오르내림
    round_index: int = 0     # 현재 몇 번째 라운드인지 (0부터 셈: 0=식사)
    finished: bool = False   # 게임이 끝났는지 여부
    # 선택 기록 목록. 리스트/딕셔너리 같은 "가변 값"의 기본값은
    # field(default_factory=list)로 지정해야 세션끼리 기록이 섞이지 않아요.
    history: list[dict[str, Any]] = field(default_factory=list)


# 모든 세션을 담아두는 저장소. {세션ID: Session객체} 형태의 딕셔너리입니다.
# ⚠️ 서버 메모리에만 존재하므로 서버를 재시작하면 사라집니다.
#    실서비스에서는 Redis 같은 외부 저장소로 교체하는 걸 권장해요.
SESSIONS: dict[str, Session] = {}


def _get_session(session_id: str) -> Session:
    """세션 ID로 세션을 찾아 돌려주는 도우미 함수. 없으면 에러를 냅니다.

    함수 이름 앞의 밑줄(_)은 "이 파일 내부에서만 쓰는 함수"라는 관례 표시예요.
    (tool로 등록되지 않으므로 LLM은 호출할 수 없습니다.)
    """
    session = SESSIONS.get(session_id)  # .get()은 키가 없으면 None을 돌려줌
    if session is None:
        # ToolError로 raise하면 이 메시지가 LLM에게 그대로 전달됩니다.
        # 그래서 에러 문구도 "다음에 뭘 해야 하는지" 안내하도록 썼어요.
        raise ToolError(
            f"세션 '{session_id}'를 찾을 수 없습니다. start_date로 새 세션을 시작하세요."
        )
    return session


def _clamp(value: int, low: int, high: int) -> int:
    """값을 low~high 범위 안에 가두는 도우미 함수.

    예: _clamp(150, 0, 100) → 100 / _clamp(-5, 0, 100) → 0
    LLM이 규칙을 벗어난 점수(예: +50)를 보내도 서버가 강제로 범위를
    지키게 만드는 안전장치입니다.
    """
    return max(low, min(high, value))



def _text_has_any(text: str, keywords: list[str]) -> bool:
    """persona/relationship_stage의 키워드 기반 간단 분류용 helper."""
    lowered = text.lower()
    return any(k.lower() in lowered for k in keywords)


def _event_bonus_score(event_bonus: str, persona: str, relationship_stage: str) -> int:
    """이벤트 보너스를 상대 페르소나와 관계 단계에 맞게 보정합니다.

    이 함수는 LLM 없이 서버 로직만으로 동작하는 규칙 기반 보정입니다.
    단, persona가 자유 텍스트이므로 완전한 자연어 이해가 아니라 키워드 기반 휴리스틱입니다.
    더 섬세한 판단은 make_choice의 taste_match_delta에서 LLM이 담당하고,
    서버는 event_bonus가 과하게 유리해지지 않도록 보정·클램핑합니다.
    """
    if event_bonus not in EVENT_BONUS_BASE:
        raise ToolError(f"event_bonus는 {list(EVENT_BONUS_BASE)} 중 하나여야 합니다.")

    score = EVENT_BONUS_BASE[event_bonus]
    profile = f"{persona} {relationship_stage}"

    # 성격/취향 키워드. persona가 자유 텍스트라서 LLM 없이 할 수 있는 판단은 이 정도가 한계입니다.
    is_shy = _text_has_any(profile, ["소심", "내향", "수줍", "낯가림", "부담", "조심", "천천히", "신중"])
    is_expressive = _text_has_any(profile, ["활발", "외향", "표현", "직진", "로맨틱", "애교", "감성", "서프라이즈", "이벤트"])
    is_practical = _text_has_any(profile, ["실용", "현실", "검소", "담백", "필요한", "쓸모", "과한거 싫", "과한 것 싫", "부담 싫"])
    is_sentimental = _text_has_any(profile, ["감성", "편지", "기록", "추억", "사진", "기념", "의미", "소소"])
    is_independent = _text_has_any(profile, ["독립", "자기주도", "간섭 싫", "부담 싫", "취향 확실", "직접 고르는"])
    likes_cute_items = _text_has_any(profile, ["귀여운", "소품", "문구", "키링", "인형", "아기자기", "캐릭터"])

    early_stage = _text_has_any(relationship_stage, ["썸", "첫", "초반", "1~3개월"])
    long_stage = _text_has_any(relationship_stage, ["1년", "장기", "오래", "기념일"])

    if event_bonus == "gift_direct":
        # 직접 표현은 외향/로맨틱 성향에는 좋지만, 초반·소심한 상대에게는 부담이 될 수 있음.
        if is_shy or early_stage:
            score -= 3
        if is_expressive:
            score += 3
        if long_stage:
            score += 1

    elif event_bonus == "gift_quiet":
        # 조용히 챙기는 방식은 부담을 줄여서 대부분 무난하지만, 표현을 좋아하는 상대에게는 덜 강렬할 수 있음.
        if is_shy or early_stage:
            score += 3
        if is_practical:
            score += 1
        if is_expressive and not early_stage:
            score += 1
        if likes_cute_items:
            score += 1

    elif event_bonus == "gift_later":
        # 지금 바로 사기보다 기억해두는 방식. 초반/소심/실용 성향에게 특히 안정적.
        if is_shy or is_practical or early_stage:
            score += 3
        if is_sentimental or long_stage:
            score += 2
        if is_expressive and not long_stage:
            score -= 1

    elif event_bonus == "gift_together":
        # 같이 고르는 방식. 상대 취향을 존중하지만, 깜짝 감동은 조금 약할 수 있음.
        if is_independent or is_practical:
            score += 3
        if early_stage:
            score += 1
        if is_shy:
            score += 1
        if is_expressive and long_stage:
            score += 1

    elif event_bonus == "gift_practical":
        # 필요한 걸 챙기는 방식. 실용/검소 성향에게 강함. 감성형에게는 다소 덜 설렐 수 있음.
        if is_practical:
            score += 4
        if is_shy or early_stage:
            score += 1
        if is_sentimental and not is_practical:
            score -= 1

    elif event_bonus == "gift_note":
        # 가격보다 의미를 더하는 방식. 감성/기념/장기연애에는 강하지만 너무 초반이면 과할 수 있음.
        if is_sentimental:
            score += 4
        if long_stage:
            score += 2
        if early_stage and not is_sentimental:
            score -= 1
        if is_practical and not is_sentimental:
            score -= 1

    return _clamp(score, -4, 5)

def _pick_unused_random_event(session: Session) -> str | None:
    """한 세션 안에서 이전에 나온 돌발 이벤트를 제외하고 하나를 뽑습니다."""
    available = [e for e in RANDOM_EVENTS if e not in session.used_random_events]
    if not available:
        return None
    event = random.choice(available)
    session.used_random_events.add(event)
    return event


# ===========================================================================
# [기능 4] 장소 검색 (카카오 로컬 API 전용 — 목업 없음)
# ===========================================================================
# "이 게임의 배경이 진짜 우리 동네"가 되게 해주는 핵심 기능.
# 카카오 로컬 API에 검색어를 보내면 실제 가게 목록을 받아옵니다.
# 키가 없으면 위에서 이미 서버가 시작을 거부했으므로, 여기서는
# "API 호출이 실패하는 경우"만 신경 쓰면 됩니다.


async def _kakao_keyword_search(params: dict[str, Any]) -> list[dict[str, Any]]:
    """카카오 로컬 키워드 검색 공통 호출 함수.

    API 절약을 위해 동일한 파라미터 검색은 서버 메모리 캐시를 우선 사용합니다.
    캐시는 서버 프로세스가 살아있는 동안만 유지됩니다.
    """
    cache_key = tuple(sorted((str(k), str(v)) for k, v in params.items()))
    if cache_key in KAKAO_SEARCH_CACHE:
        return KAKAO_SEARCH_CACHE[cache_key]

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                KAKAO_LOCAL_URL,
                params=params,
                headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"},
            )
            resp.raise_for_status()
            documents = resp.json().get("documents", [])
    except httpx.HTTPStatusError as e:
        raise ToolError(
            f"장소 검색 API 오류 (HTTP {e.response.status_code}). "
            "잠시 후 다시 시도하거나 다른 검색어로 재시도하세요."
        )
    except httpx.HTTPError:
        raise ToolError("장소 검색 중 네트워크 오류가 발생했습니다. 같은 검색어로 한 번 더 시도하세요.")

    if len(KAKAO_SEARCH_CACHE) >= MAX_CACHE_ENTRIES:
        # 가장 단순한 캐시 정리: 오래된 첫 항목 제거. 실서비스에서는 TTL/LRU 캐시 권장.
        KAKAO_SEARCH_CACHE.pop(next(iter(KAKAO_SEARCH_CACHE)))
    KAKAO_SEARCH_CACHE[cache_key] = documents
    return documents

async def _resolve_meeting_center(meeting_place: str) -> dict[str, str]:
    """만남 장소명을 카카오 로컬 API로 검색해 기준 좌표를 얻습니다."""
    documents = await _kakao_keyword_search({"query": meeting_place, "size": 1})
    if not documents:
        raise ToolError(
            f"만남 장소 '{meeting_place}'의 위치를 찾지 못했습니다. "
            "예: '홍대입구역', '성수역', '강남역 11번 출구'처럼 더 구체적인 장소명으로 start_date를 다시 시작하세요."
        )

    first = documents[0]
    x = first.get("x")
    y = first.get("y")
    if not x or not y:
        raise ToolError(
            f"만남 장소 '{meeting_place}'의 좌표를 확인하지 못했습니다. "
            "역명이나 유명 장소명으로 다시 입력해 주세요."
        )

    return {
        "x": x,
        "y": y,
        "place_name": first.get("place_name", meeting_place),
    }


def _place_key(document: dict[str, Any]) -> str:
    """장소 중복 제거용 식별자를 만듭니다.

    카카오 Local API가 id를 주면 id를 우선 사용하고, 없으면 장소명+주소 조합으로
    세션 안에서 같은 장소가 반복 노출되는 것을 줄입니다.
    """
    place_id = document.get("id")
    if place_id:
        return f"id:{place_id}"
    name = (document.get("place_name") or "").strip().lower()
    address = (document.get("road_address_name") or document.get("address_name") or "").strip().lower()
    return f"fallback:{name}|{address}"


def _format_place(document: dict[str, Any]) -> dict[str, str]:
    """카카오 API 응답을 LLM에게 넘기기 좋은 장소 딕셔너리로 정리합니다."""
    return {
        "place_key": _place_key(document),
        "place_name": document.get("place_name", ""),
        "category_name": document.get("category_name", ""),
        "road_address_name": document.get("road_address_name") or document.get("address_name", ""),
        "distance_m": document.get("distance", ""),
    }


def _distance_as_int(place: dict[str, str]) -> int:
    """distance_m 문자열을 정렬 가능한 정수로 바꿉니다."""
    try:
        return int(place.get("distance_m") or 999999)
    except (TypeError, ValueError):
        return 999999


async def _search_places(
    query: str,
    *,
    center_x: str,
    center_y: str,
    exclude_place_keys: set[str] | None = None,
    size: int = SEARCH_RETURN_SIZE,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """카카오 로컬 API로 기준 좌표 주변 장소를 검색합니다.

    API 절약형 버전입니다.
    - 기본은 keyword 1개당 1페이지, 1.2km 반경만 조회합니다.
    - fresh 후보가 부족할 때만 1.6km → 2.0km로 반경을 넓힙니다.
    - SEARCH_MAX_PAGES 기본값은 1로 두고, 운영자가 환경변수로만 늘릴 수 있게 합니다.
    - 같은 파라미터 검색은 _kakao_keyword_search의 메모리 캐시로 재사용합니다.
    """
    exclude_place_keys = exclude_place_keys or set()
    all_candidates: list[dict[str, str]] = []
    fallback_candidates: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    used_radius = RADIUS_EXPANSION_STEPS[0]
    api_request_slots = 0

    for radius_m in RADIUS_EXPANSION_STEPS:
        used_radius = radius_m
        radius_candidates: list[dict[str, str]] = []

        for page in range(1, SEARCH_MAX_PAGES + 1):
            api_request_slots += 1
            params: dict[str, Any] = {
                "query": query,
                "x": center_x,
                "y": center_y,
                "radius": radius_m,
                "sort": "distance",
                "size": SEARCH_PAGE_SIZE,
                "page": page,
            }
            documents = await _kakao_keyword_search(params)
            if not documents:
                continue

            for d in documents:
                key = _place_key(d)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                formatted = _format_place(d)
                fallback_candidates.append(formatted)

                if key not in exclude_place_keys:
                    radius_candidates.append(formatted)

        all_candidates.extend(radius_candidates)

        # 후보가 충분하면 더 넓은 반경을 호출하지 않고 종료합니다.
        if len(all_candidates) >= MIN_FRESH_RESULTS:
            break

    reused_seen_places = False
    if not all_candidates:
        all_candidates = fallback_candidates
        reused_seen_places = True

    if not all_candidates:
        raise ToolError(
            f"'{query}' 검색 결과가 기준 장소 주변에서 없습니다. "
            "더 짧고 일반적인 검색어로 재시도하세요 (예: '성수역 감성 파스타 맛집' → '성수 파스타', '성수 맛집')."
        )

    all_candidates.sort(key=_distance_as_int)
    pool_size = min(len(all_candidates), max(size * 2, 8))
    candidate_pool = all_candidates[:pool_size]
    random.shuffle(candidate_pool)
    selected = candidate_pool[: min(size, len(candidate_pool))]
    selected.sort(key=_distance_as_int)

    meta = {
        "used_radius_m": used_radius,
        "base_radius_m": NEARBY_RADIUS_M,
        "fresh_candidate_count": 0 if reused_seen_places else len(all_candidates),
        "returned_count": len(selected),
        "excluded_seen_count": len(exclude_place_keys),
        "reused_seen_places": reused_seen_places,
        "api_request_slots": api_request_slots,
        "cache_policy": "동일 검색 파라미터는 서버 메모리 캐시를 재사용합니다.",
        "diversity_policy": (
            "이번 세션에서 이미 보여준 장소는 제외하고, 가까운 후보 풀 안에서 섞어 반환합니다. "
            "API 절약을 위해 기본 1페이지를 먼저 사용하고 후보가 부족할 때만 반경을 넓힙니다."
        ),
    }
    return selected, meta

def _keyword_family(round_name: str, keyword: str | None) -> str:
    """검색 키워드를 '분야' 단위로 묶습니다.

    목적은 한 라운드에서 영화관 3개, 공방 3개처럼 같은 분야만 반복되는 것을 막는 것입니다.
    예를 들어 액티비티 라운드는 '영화/공연', '만들기/클래스', '사진/기록',
    '구경/전시', '놀이/게임'처럼 서로 다른 분야를 섞어 검색합니다.
    """
    if not keyword:
        return "사용자검색"

    k = keyword.lower()
    families_by_round = {
        "식사": {
            "한식/밥집": ["한식", "조용한 밥집", "백반", "고기집", "샤브샤브"],
            "양식/브런치": ["양식", "브런치", "캐주얼 다이닝", "파스타", "피자", "버거", "샌드위치", "리조또"],
            "일식": ["일식", "라멘", "돈카츠", "초밥", "우동", "오므라이스", "이자카야"],
            "중식/아시안": ["중식", "쌀국수", "태국", "베트남", "인도", "멕시칸", "딤섬"],
            "가벼운식사": ["포케", "샐러드", "비건", "분식"],
        },
        "액티비티": {
            "구경/전시": ["팝업", "전시", "서점", "독립서점", "편집샵", "플리마켓"],
            "사진/기록": ["셀프사진", "네컷", "사진관"],
            "만들기/클래스": ["공방", "도자기", "향수", "가죽", "캔들", "원데이클래스"],
            "놀이/게임": ["오락실", "아케이드", "볼링", "만화카페", "보드게임", "방탈출", "실내 스포츠", "vr"],
            "영화/공연/음악": ["영화", "소극장", "공연", "lp바", "재즈바"],
        },
        "카페·소품샵": {
            "카페/대화": ["카페", "조용한", "감성", "티룸", "북카페", "로스터리"],
            "디저트/베이커리": ["디저트", "베이커리", "푸딩", "젤라또", "아이스크림", "케이크"],
            "소품/편집샵": ["소품", "문구", "편집샵", "라이프스타일", "잡화", "캐릭터", "선물"],
            "감성선물": ["플라워", "향수", "레코드", "빈티지"],
        },
        "마무리": {
            "귀가동선": ["지하철", "버스", "택시", "역", "정류장"],
            "산책/대화": ["산책", "공원", "하천", "골목", "광장"],
            "여운/사진": ["야경", "전망", "포토스팟", "포토부스"],
            "작은배려": ["베이커리", "디저트", "편의점"],
        },
    }

    for family, words in families_by_round.get(round_name, {}).items():
        if any(word.lower() in k for word in words):
            return family
    return keyword


def _select_auto_keywords(session: Session, round_name: str, query: str | None = None) -> tuple[list[str], list[str]]:
    """현재 라운드에 맞는 검색어를 서버가 직접 고릅니다.

    핵심은 '검색어 개수'만 늘리는 것이 아니라, 서로 다른 분야의 키워드를 섞는 것입니다.
    예를 들어 액티비티 라운드에서 영화관 후보만 여러 개 보여주는 대신,
    영화/공연 + 만들기/클래스 + 구경/전시처럼 다른 분야를 한 번에 검색합니다.

    Returns:
        queries: Kakao Local API에 실제로 넣을 완성 검색어 목록
        keywords: 이번 호출에서 새로 사용한 자동 키워드 목록
    """
    pool = ROUND_KEYWORD_POOLS.get(round_name, [])
    used = session.used_search_keywords

    unused_keywords = [kw for kw in pool if f"{round_name}:{kw}" not in used]
    if not unused_keywords:
        unused_keywords = pool[:]

    random.shuffle(unused_keywords)

    has_manual_query = bool(query and query.strip())
    auto_limit = max(1, AUTO_KEYWORD_COUNT - 1) if has_manual_query else AUTO_KEYWORD_COUNT

    picked_keywords: list[str] = []
    picked_families: set[str] = set()

    # 1차: 서로 다른 분야의 키워드를 우선 선택합니다.
    for kw in unused_keywords:
        family = _keyword_family(round_name, kw)
        if family in picked_families:
            continue
        picked_keywords.append(kw)
        picked_families.add(family)
        if len(picked_keywords) >= auto_limit:
            break

    # 2차: 분야가 부족하면 남은 키워드로 채웁니다.
    if len(picked_keywords) < auto_limit:
        for kw in unused_keywords:
            if kw in picked_keywords:
                continue
            picked_keywords.append(kw)
            if len(picked_keywords) >= auto_limit:
                break

    for kw in picked_keywords:
        used.add(f"{round_name}:{kw}")

    queries: list[str] = []
    if has_manual_query:
        queries.append(query.strip())

    for kw in picked_keywords:
        queries.append(f"{session.meeting_place} {kw}")

    deduped_queries = list(dict.fromkeys(queries))
    return deduped_queries, picked_keywords

def _score_place_for_round(place: dict[str, str], *, round_name: str, query_keyword: str | None = None) -> dict[str, Any]:
    """장소 후보를 서버 로직으로 1차 점수화합니다.

    이 점수는 최종 호감도 점수가 아니라, search_nearby가 어떤 장소를 먼저 보여줄지
    정하는 내부 추천 점수입니다. LLM description에만 맡기지 않고 MCP가 거리·다양성·
    라운드 적합성을 반영하도록 만든 부분입니다.
    """
    distance = _distance_as_int(place)

    if distance <= 400:
        distance_score = 5
    elif distance <= 800:
        distance_score = 4
    elif distance <= 1200:
        distance_score = 3
    elif distance <= 1600:
        distance_score = 1
    else:
        distance_score = -2

    category_text = f"{place.get('place_name', '')} {place.get('category_name', '')} {query_keyword or ''}"

    # 라운드별로 카카오 카테고리/장소명에 이런 단어가 들어가면 조금 더 잘 맞는 후보로 봅니다.
    fit_keywords = {
        "식사": ["음식", "식당", "한식", "양식", "일식", "중식", "분식", "카레", "쌀국수", "라멘", "브런치"],
        "액티비티": ["전시", "공방", "사진", "영화", "게임", "볼링", "서점", "팝업", "공연", "체험", "클래스"],
        "카페·소품샵": ["카페", "디저트", "베이커리", "소품", "문구", "편집샵", "꽃", "플라워", "잡화"],
        "마무리": ["공원", "산책", "역", "정류장", "전망", "야경", "포토", "광장", "하천"],
    }.get(round_name, [])

    fit_score = 2 if _text_has_any(category_text, fit_keywords) else 0

    # 너무 먼 후보는 라운드 적합성이 있어도 감점합니다.
    score = distance_score + fit_score

    reasons = []
    reasons.append(f"거리점수 {distance_score}")
    if fit_score:
        reasons.append("라운드 적합도 +2")
    if query_keyword:
        reasons.append(f"검색키워드 '{query_keyword}'")

    return {
        "score": score,
        "reasons": reasons,
    }



def _place_display_family(place: dict[str, Any], *, round_name: str) -> str:
    """최종 사용자 선택지에 보여줄 '분야'를 분류합니다.

    식사는 음식 계열, 액티비티는 활동 유형, 카페·소품샵은 휴식/구경 유형,
    마무리는 끝맺음 방식으로 묶습니다. 이 값은 같은 라운드에서 한 분야만 몰려
    보이는 문제를 줄이는 데 사용됩니다.
    """
    text = f"{place.get('place_name', '')} {place.get('category_name', '')} {place.get('source_keyword', '')}"

    families_by_round = {
        "식사": {
            "분식/김밥": ["김밥", "떡볶", "분식"],
            "면/라멘": ["라멘", "우동", "쌀국수", "국수", "면"],
            "한식": ["한식", "백반", "밥집", "국밥", "찌개", "고기", "샤브"],
            "양식": ["파스타", "피자", "리조또", "양식", "브런치", "버거"],
            "일식": ["초밥", "스시", "돈카츠", "일식", "덮밥", "오므라이스"],
            "중식": ["중식", "딤섬", "마라", "훠궈"],
            "아시안": ["태국", "베트남", "커리", "인도", "멕시칸"],
            "가벼운식사": ["샐러드", "포케", "샌드위치", "비건"],
        },
        "액티비티": {
            "구경/전시": ["전시", "팝업", "서점", "독립서점", "편집샵", "플리마켓"],
            "사진/기록": ["셀프사진", "네컷", "사진관", "포토"],
            "만들기/클래스": ["공방", "도자기", "향수", "가죽", "캔들", "원데이", "클래스", "체험"],
            "놀이/게임": ["오락실", "아케이드", "볼링", "만화카페", "보드게임", "방탈출", "vr", "스포츠"],
            "영화/공연/음악": ["영화", "극장", "소극장", "공연", "재즈", "lp바", "음악"],
        },
        "카페·소품샵": {
            "카페/대화": ["카페", "티룸", "북카페", "로스터리", "커피"],
            "디저트/베이커리": ["디저트", "베이커리", "푸딩", "젤라또", "아이스크림", "케이크", "빵"],
            "소품/편집샵": ["소품", "문구", "편집샵", "라이프스타일", "잡화", "캐릭터", "선물"],
            "감성선물": ["플라워", "꽃", "향수", "레코드", "빈티지"],
        },
        "마무리": {
            "귀가동선": ["지하철", "역", "버스", "정류장", "택시"],
            "산책/대화": ["산책", "공원", "하천", "골목", "광장", "벤치"],
            "여운/사진": ["야경", "전망", "포토", "사진"],
            "작은배려": ["베이커리", "디저트", "편의점", "음료"],
        },
    }

    for family, keywords in families_by_round.get(round_name, {}).items():
        if _text_has_any(text, keywords):
            return family

    # 그래도 매칭이 안 되면 검색 키워드의 분야를 사용합니다.
    return _keyword_family(round_name, str(place.get("source_keyword") or "기타"))


def _diversify_places_for_display(candidates: list[dict[str, Any]], *, round_name: str, size: int) -> list[dict[str, Any]]:
    """최종 선택지에서 같은 분야가 과하게 반복되지 않도록 분산합니다.

    사용자가 원하는 것은 '영화관 중 어디 갈까?'가 아니라
    '영화/공연, 공방, 전시, 사진관처럼 서로 다른 데이트 방식 중 무엇을 고를까?'입니다.
    그래서 최종 places는 같은 display_family가 최대한 1개씩만 보이도록 먼저 고르고,
    후보가 부족할 때만 제한을 완화합니다.
    """
    if not candidates:
        return []

    candidates = sorted(
        candidates,
        key=lambda p: (p.get("sumroute_place_score", 0), -_distance_as_int(p)),
        reverse=True,
    )

    selected: list[dict[str, Any]] = []
    used_keys: set[str] = set()
    family_count: dict[str, int] = {}
    keyword_count: dict[str, int] = {}

    # 1차에서는 모든 라운드에서 분야당 1개를 목표로 합니다.
    # 후보가 부족할 때만 2차에서 제한을 풀어 채웁니다.
    strict_max_per_family = 1
    strict_max_per_keyword = 1

    def annotate(place: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(place)
        enriched["display_family"] = _place_display_family(enriched, round_name=round_name)
        return enriched

    candidates = [annotate(p) for p in candidates]

    def can_pick(place: dict[str, Any], *, strict: bool) -> bool:
        key = place.get("place_key") or f"{place.get('place_name')}|{place.get('road_address_name')}"
        if key in used_keys:
            return False
        family = str(place.get("display_family") or "기타")
        keyword = str(place.get("source_keyword") or place.get("source_query") or "user_query")
        if strict:
            if family_count.get(family, 0) >= strict_max_per_family:
                return False
            if keyword_count.get(keyword, 0) >= strict_max_per_keyword:
                return False
        return True

    def pick(place: dict[str, Any]) -> None:
        key = place.get("place_key") or f"{place.get('place_name')}|{place.get('road_address_name')}"
        family = str(place.get("display_family") or "기타")
        keyword = str(place.get("source_keyword") or place.get("source_query") or "user_query")
        selected.append(place)
        used_keys.add(key)
        family_count[family] = family_count.get(family, 0) + 1
        keyword_count[keyword] = keyword_count.get(keyword, 0) + 1

    # 1차: 분야가 서로 다른 후보를 우선 선택
    for place in candidates:
        if len(selected) >= size:
            break
        if can_pick(place, strict=True):
            pick(place)

    # 2차: 후보가 부족하면 제한을 풀어 채움
    for place in candidates:
        if len(selected) >= size:
            break
        if can_pick(place, strict=False):
            pick(place)

    return selected[:size]

async def _search_places_for_round(
    session: Session,
    round_name: str,
    queries: list[str],
    keywords: list[str],
    *,
    size: int = SEARCH_RETURN_SIZE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """여러 자동 검색어를 실행하고, 서버가 후보를 합쳐 점수화합니다."""
    if session.center_x is None or session.center_y is None:
        raise ToolError("검색 기준 좌표가 설정되지 않았습니다. search_nearby를 다시 호출해 주세요.")

    combined: dict[str, dict[str, Any]] = {}
    query_metas: list[dict[str, Any]] = []
    errors: list[str] = []

    for idx, q in enumerate(queries):
        # query가 사용자 입력이면 keyword가 없을 수 있음. 자동 검색어는 keywords 순서와 대략 대응됩니다.
        keyword = None
        for kw in keywords:
            if kw in q:
                keyword = kw
                break

        try:
            places, meta = await _search_places(
                q,
                center_x=session.center_x,
                center_y=session.center_y,
                exclude_place_keys=session.shown_place_keys,
                size=max(size, 6),
            )
        except ToolError as e:
            errors.append(str(e))
            continue

        query_metas.append({"query": q, **meta})

        for place in places:
            key = place.get("place_key") or f"fallback:{place.get('place_name')}|{place.get('road_address_name')}"
            scored = _score_place_for_round(place, round_name=round_name, query_keyword=keyword)

            if key not in combined or scored["score"] > combined[key].get("sumroute_place_score", -999):
                enriched = dict(place)
                enriched["source_query"] = q
                enriched["source_keyword"] = keyword or "user_query"
                enriched["sumroute_place_score"] = scored["score"]
                enriched["score_reasons"] = scored["reasons"]
                combined[key] = enriched

    if not combined:
        hint = "; ".join(errors[:2]) if errors else "검색 결과가 없습니다."
        raise ToolError(
            f"현재 라운드({round_name})에서 사용할 만한 장소 후보를 찾지 못했습니다. {hint} "
            "다시 시도하면 서버가 다른 자동 키워드를 선택합니다."
        )

    candidates = list(combined.values())

    # 점수 우선 후보 풀을 만들되, 최종 표시 후보는 source_keyword/음식 유형이 한쪽으로 몰리지 않게 다시 분산합니다.
    candidates.sort(key=lambda p: (p.get("sumroute_place_score", 0), -_distance_as_int(p)), reverse=True)
    top_pool = candidates[: min(len(candidates), max(size * 4, 12))]
    random.shuffle(top_pool)
    top_pool.sort(key=lambda p: (p.get("sumroute_place_score", 0), -_distance_as_int(p)), reverse=True)
    selected = _diversify_places_for_display(top_pool, round_name=round_name, size=min(size, len(top_pool)))

    meta = {
        "auto_keywords_used": keywords,
        "queries_used": queries,
        "estimated_api_request_slots": sum(m.get("api_request_slots", 0) for m in query_metas),
        "query_metas": query_metas,
        "candidate_count_after_merge": len(candidates),
        "returned_count": len(selected),
        "display_diversity_policy": "최종 선택지는 source_keyword뿐 아니라 display_family 기준으로도 분산합니다. 예: 영화관 여러 개가 아니라 영화/공연, 공방, 전시, 사진관처럼 다른 분야가 섞이도록 합니다.",
        "server_scoring_policy": (
            "MCP server generates round-specific search keywords, merges Kakao results, "
            "excludes already shown places, and ranks candidates by distance and round fit. "
            "LLM should explain the options, not invent the candidate pool."
        ),
    }
    return selected, meta


# ===========================================================================
# [기능 5] Tool ① start_date — 게임 시작
# ===========================================================================
# annotations: PlayMCP 가이드 필수 property. 상태를 새로 만드므로 destructive.


@mcp.tool(
    name = "start_date",
    description = 
    """
    Start a new sumroute(썸루트) date rehearsal session.
    Use this first when the user wants to simulate a date course around a real meeting place.
    This tool stores the partner personality/taste, relationship stage, and meeting place, then returns
    the first route round. Do not call this tool until the assistant has collected all required inputs:
    partner_gender, partner_personality_and_taste, relationship_stage, and meeting_place.
    The assistant should roleplay the partner lightly and guide the user through realistic route choices
    rather than asking for exact prices or private details.
    """,
    annotations = {
        "title": "Start SumRoute Date Rehearsal",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def start_date(
    partner_gender: str,
    partner_personality_and_taste: str,
    meeting_place: str,
    relationship_stage: str,
) -> dict[str, Any]:
    """
    Args:
        partner_gender: Partner's gender ("남" / "여" / "기타")
        partner_personality_and_taste: REQUIRED. Partner's personality, taste, dislikes, and date preference as free text
            (e.g. "소심하고 조용한 편, 매운 음식 싫어함, 귀여운 소품 좋아함")
        meeting_place: Meeting spot (e.g. "홍대입구역") — anchor point for place search
        relationship_stage: How long the couple has been together
            ("썸" / "1~3개월" / "1년차" / "장기연애")
    """
    # Create a new game session.
    session = Session(
        session_id=uuid.uuid4().hex[:12],  # short random ID
        partner_gender=partner_gender,
        meeting_place=meeting_place,
        persona=partner_personality_and_taste,
        relationship_stage=relationship_stage,
    )
    SESSIONS[session.session_id] = session  # store for later lookup

    # "roleplay_instruction" steers what the LLM should act out next.
    return {
        "session_id": session.session_id,
        "total_rounds": len(ROUNDS),
        # {"number": 1, **ROUNDS[0]}의 ** 문법: ROUNDS[0] 딕셔너리의 내용물을
        # 통째로 풀어서 새 딕셔너리에 합쳐 넣는다는 뜻이에요.
        "current_round": {"number": 1, **ROUNDS[0]},
        "roleplay_instruction": (
            f"지금부터 당신은 사용자의 데이트 상대({partner_gender})입니다. "
            f"상대 성격/취향: '{partner_personality_and_taste}'. 만난 기간: {relationship_stage}. "
            f"페르소나에 맞는 말투와 텐션을 처음부터 끝까지 일관되게 유지하세요. "
            f"먼저 {meeting_place}에서 만나는 장면을 짧게 연출하고 "
            f"(만난 기간에 어울리는 첫 인사 톤으로), search_nearby로 근처 식당을 "
            f"검색해 1라운드 선택지를 제시하세요. "
            f"코스는 {NEARBY_RADIUS_M}m 반경, 즉 지하철 한 정거장 내외에서 이어지게 설계하세요. "
            f"호감도 점수는 절대 직접 말하지 말고 표정과 말투로만 표현하세요."
        ),
    }


# ===========================================================================
# [기능 6] Tool ② search_nearby — 근처 장소 검색
# ===========================================================================
# annotations: 조회만 하므로 readOnly. 외부(카카오) API에 의존하므로 openWorld.


@mcp.tool(
    name = "search_nearby",
    description = 
    """
    Search real nearby places for the current sumroute(썸루트) round using Kakao Local API.

    This tool is intentionally designed to reduce LLM prompt dependence. The assistant may pass
    a short optional query, but it does not need to craft every search keyword. The MCP server
    automatically checks the current round, selects unused category keywords from an internal
    round-specific keyword pool, calls Kakao Local API multiple times, merges candidates, excludes
    places already shown in this session, and ranks places by distance and round fit.

    Use this after start_date and before presenting route choices to the user.
    Optional query rule: if provided, keep it short, like '성수 브런치' or '홍대 셀프사진관'.
    Do not pass long sentences, budget details, full persona text, or many conditions.

    The returned places already include server-side metadata such as source_keyword,
    sumroute_place_score, score_reasons, distance_m, and search_meta. Present 3~4 realistic
    choices based on these returned places. Do not invent additional places outside the tool result.
    """,
    annotations={
        "title": "Search SumRoute Places",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def search_nearby(
    session_id: str,
    query: str | None = None,
) -> dict[str, Any]:
    """
    Args:
        session_id: Session ID issued by start_date
        query: Optional short override query. If omitted, the server automatically generates
            multiple round-specific search queries using the meeting_place and internal keyword pool.
            Examples: "성수 브런치", "홍대 셀프사진관", "연남 티룸".
    """
    session = _get_session(session_id)
    if session.finished:
        raise ToolError("이미 종료된 세션입니다. get_result를 호출하거나 새 세션을 시작하세요.")

    round_info = ROUNDS[session.round_index]
    round_key = round_info["name"]

    # 최초 검색 시 meeting_place를 좌표로 해석하고 세션에 저장합니다.
    # 이후 모든 장소 검색은 이 기준점 반경 안에서 수행합니다.
    if session.center_x is None or session.center_y is None:
        center = await _resolve_meeting_center(session.meeting_place)
        session.center_x = center["x"]
        session.center_y = center["y"]
        session.center_place_name = center["place_name"]

    session.search_count_by_round[round_key] = session.search_count_by_round.get(round_key, 0) + 1

    # 핵심 변경점: LLM이 직접 query를 만들지 않아도 서버가 라운드별 자동 키워드를 선택합니다.
    queries, auto_keywords = _select_auto_keywords(session, round_key, query)

    places, search_meta = await _search_places_for_round(
        session,
        round_key,
        queries,
        auto_keywords,
        size=SEARCH_RETURN_SIZE,
    )

    # 이번에 사용자에게 보여준 장소를 세션에 기록해 다음 검색에서 반복 노출을 줄입니다.
    for place in places:
        if place.get("place_key"):
            session.shown_place_keys.add(place["place_key"])

    return {
        "session_id": session_id,
        "current_round": {"number": session.round_index + 1, **round_info},
        "search_center": {
            "meeting_place_input": session.meeting_place,
            "resolved_place_name": session.center_place_name,
            "base_radius_m": NEARBY_RADIUS_M,
            "radius_expansion_steps": RADIUS_EXPANSION_STEPS,
            "search_count_this_round": session.search_count_by_round[round_key],
            "policy": "기본은 지하철 한 정거장 내외 동선. 후보 부족 시에만 반경을 단계적으로 확장합니다.",
        },
        "search_meta": search_meta,
        "places": places,
        "presentation_instruction": (
            "위 places에 들어있는 후보만 사용해 3~4개 선택지를 제시하세요. "
            "장소를 새로 invent하지 말고, source_keyword와 score_reasons를 참고해 왜 이 후보가 나왔는지 자연스럽게 설명하세요. "
            "각 선택지에는 가능하면 distance_m를 활용해 기준 장소에서 얼마나 가까운지도 짧게 언급하세요. "
            "sumroute_place_score가 높은 후보를 우선 제시하되, display_family가 서로 다른 선택지를 섞으세요. 예를 들어 액티비티는 영화관만 여러 개 보여주지 말고 영화/공연·공방·전시·사진관처럼 분야가 다른 후보를 비교하세요. "
            "auto_keywords_used는 MCP 서버가 이번 라운드에서 자동으로 고른 검색 키워드입니다. "
            "사용자가 고르면 make_choice를 호출하세요. "
            "라운드 사이에 필요하면 짧은 대화 선택지도 끼워 넣을 수 있지만, 실제 장소 코스가 아닌 대화 선택은 advance_round=False로 기록하세요."
        ),
    }


# ===========================================================================
# [기능 7] Tool ③ make_choice — 선택 기록 + 호감도 갱신 (게임의 심장부)
# ===========================================================================
# annotations: 호감도 상태를 바꾸므로 not readOnly. 같은 입력도 라운드 진행에
# 따라 결과가 달라지므로 not idempotent.


@mcp.tool(
    name = "make_choice",
    description =
    """
    Record the user's selected sumroute(썸루트) step or mid-date action, then update the hidden date score.
    Use this whenever the user chooses a place, activity, reply, gift style, or crisis response.
    For actual route steps, keep advance_round=True so the choice appears in the final selected course.
    For small dialogue choices, gift style choices, or crisis responses inside the same round, set advance_round=False.
    Judge taste_match_delta conservatively by how well the choice fits the partner persona, relationship stage,
    route flow, and situation. Gift choices should use expression-style options only: gift_quiet, gift_direct, gift_later, gift_together, gift_practical, or gift_note. Do not present a no-gift / just-pass option as a normal option.
    Random crisis events do not change the score automatically; the user's response should be recorded with
    crisis_handled or crisis_mishandled only when clearly appropriate. Do not expose the hidden score unless reveal_effect is returned.
    """,
    annotations={
        "title": "Record SumRoute Choice",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def make_choice(
    session_id: str,
    choice_description: str,
    taste_match_delta: int,
    judge_reason: str,
    event_bonus: str = "none",
    advance_round: bool = True,
) -> dict[str, Any]:
    """
    Args:
        session_id: Session ID
        choice_description: What the user picked (e.g. "포모도로 파스타에서 점심")
        taste_match_delta: Your conservative taste-match judgment score.
            For actual route steps: -6 ~ +7. Excellent +5~+7, good +2~+4, neutral -1~+1, weak -2~-4, mismatch -5~-6.
            For mid-round dialogue/gift/crisis choices: -4 ~ +4.
            Do not give the maximum unless the choice strongly matches persona, relationship stage, and route flow.
        judge_reason: One-line reasoning (used for best/worst pick in the final report)
        event_bonus: Set only when applicable —
            gift_quiet / gift_direct / gift_later / gift_together / gift_practical / gift_note /
            crisis_handled / crisis_mishandled / repeat_date / none.
            Do not use or invent a no-gift event bonus. Gift bonuses are adjusted by persona and relationship_stage inside the server using keyword-based rules.
        advance_round: True (default) if this choice ends the current round.
            False for a mid-round choice (dialogue choice, gift event, etc.).
    """
    session = _get_session(session_id)
    if session.finished:
        raise ToolError("이미 종료된 세션입니다.")

    if event_bonus not in EVENT_BONUS_BASE:
        raise ToolError(f"event_bonus는 {list(EVENT_BONUS_BASE)} 중 하나여야 합니다.")

    # Clamp LLM's score into a stricter range, then apply persona-aware event bonus.
    # 실제 장소 코스 선택이 점수의 중심이고, 선물/대화/돌발 이벤트는 보조 변수로만 작동하게 합니다.
    if advance_round:
        taste_match_delta = _clamp(taste_match_delta, -6, 7)
    else:
        taste_match_delta = _clamp(taste_match_delta, -4, 4)

    bonus_delta = _event_bonus_score(event_bonus, session.persona, session.relationship_stage)
    total_delta = taste_match_delta + bonus_delta

    # 중간 이벤트를 여러 번 넣어서 점수가 과하게 부풀어 오르는 것을 방지합니다.
    if advance_round:
        total_delta = _clamp(total_delta, -8, 8)
    else:
        total_delta = _clamp(total_delta, -6, 5)

    session.affection = _clamp(session.affection + total_delta, 0, 100)

    round_info = ROUNDS[session.round_index]

    # advance_round=True인 선택은 실제 데이트 코스의 한 단계로 간주합니다.
    # advance_round=False인 선택은 대화/선물/돌발상황 같은 중간 이벤트로만 기록합니다.
    route_step_order = None
    if advance_round:
        route_step_order = sum(1 for h in session.history if h.get("is_route_step")) + 1

    session.history.append(
        {
            "round": round_info["name"],
            "choice": choice_description,
            "delta": total_delta,
            "taste_match_delta": taste_match_delta,
            "bonus_delta": bonus_delta,
            "reason": judge_reason,
            "event_bonus": event_bonus,
            "is_route_step": advance_round,
            "route_step_order": route_step_order,
        }
    )

    # Only reveal the score effect on big swings; otherwise stays hidden (None).
    reveal_effect: str | None = None
    if total_delta >= REVEAL_THRESHOLD:
        reveal_effect = f"💖 +{total_delta}"
    elif total_delta <= -REVEAL_THRESHOLD:
        reveal_effect = f"💔 {total_delta}"

    # Advance round + maybe trigger a random event; end game on last round.
    random_event: str | None = None
    is_last_round = session.round_index >= len(ROUNDS) - 1
    if advance_round and not is_last_round:
        session.round_index += 1
        if random.random() < RANDOM_EVENT_CHANCE:
            random_event = _pick_unused_random_event(session)

    game_over = advance_round and is_last_round
    if game_over:
        session.finished = True

    next_round = None if game_over else {"number": session.round_index + 1, **ROUNDS[session.round_index]}

    return {
        "session_id": session_id,
        "recorded": choice_description,
        "reveal_effect": reveal_effect,   # None이면 LLM은 점수를 숨겨야 함
        "mood_hint": _mood_hint(session.affection),  # 연기용 기분 힌트
        "random_event": random_event,     # None이면 돌발 이벤트 없음
        "random_event_policy": {
            "chance": RANDOM_EVENT_CHANCE,
            "no_repeat_in_session": True,
            "score_effect": "돌발 이벤트 자체는 점수를 자동 변경하지 않습니다. 사용자의 대응을 make_choice로 기록할 때 crisis_handled(+보정) 또는 crisis_mishandled(-보정)를 사용하세요.",
        },
        "next_round": next_round,
        "game_over": game_over,
        # Instruction text is assembled conditionally below.
        "reaction_instruction": (
            "mood_hint를 참고해 상대방의 리액션을 페르소나 말투로 연기하세요. "
            "점수·호감도 수치는 절대 언급 금지. "
            + (f"단, 이번엔 '{reveal_effect}' 이펙트를 짧게 노출해도 됩니다. " if reveal_effect else "")
            + ("게임이 끝났습니다. 헤어지는 장면을 연출한 뒤 get_result를 호출해 성적표를 발급하세요."
               if game_over
               else "리액션 후 다음 라운드로 자연스럽게 넘어가세요. "
                    "random_event가 있으면 상황을 짧게 연출하고, 사용자가 어떻게 대응할지 선택하게 하세요. "
                    "대응 결과는 make_choice(advance_round=False)로 기록하되, 잘 대처하면 event_bonus='crisis_handled', "
                    "아쉬우면 event_bonus='crisis_mishandled'를 사용하세요.")
        ),
    }


def _mood_hint(affection: int) -> str:
    """Translates the hidden affection score into an acting cue (never shown to the user)."""
    if affection >= 80:
        return "완전히 마음이 열린 상태. 텐션 높고 스킨십·장난 시도 가능."
    if affection >= 60:
        return "기분 좋은 상태. 웃음이 많고 리액션이 커짐."
    if affection >= 40:
        return "무난한 상태. 예의는 지키지만 특별한 설렘은 없음."
    if affection >= 20:
        return "슬슬 지루하거나 서운한 상태. 대답이 짧아지고 휴대폰을 자주 봄."
    return "집에 가고 싶은 상태. 한숨, 단답, 시계 확인."


def _build_selected_course(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """사용자가 최종적으로 선택한 데이트 코스를 라운드 순서대로 요약합니다.

    make_choice에서 advance_round=True로 기록된 선택만 실제 코스 단계로 봅니다.
    advance_round=False로 기록된 대화 선택, 선물 이벤트, 돌발상황 대응은
    최종 코스 요약에는 섞지 않고 별도 history에만 남깁니다.
    """
    selected_course: list[dict[str, Any]] = []

    for h in history:
        # 기존 세션 호환: is_route_step 키가 없던 기록은 실제 선택으로 간주합니다.
        if not h.get("is_route_step", True):
            continue

        selected_course.append(
            {
                "order": len(selected_course) + 1,
                "round": h.get("round", ""),
                "selected": h.get("choice", ""),
                "reason": h.get("reason", ""),
                "effect": h.get("delta", 0),
                "event_bonus": h.get("event_bonus", "none"),
            }
        )

    return selected_course


def _build_course_one_liner(selected_course: list[dict[str, Any]]) -> str:
    """최종 결과 상단에 보여줄 한 줄 코스 요약을 만듭니다."""
    if not selected_course:
        return "아직 선택된 코스가 없습니다."

    return " → ".join(
        f"{step['round']}: {step['selected']}" for step in selected_course
    )


def _build_plan_b_suggestions(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """선택 기록을 바탕으로 '이렇게 했으면 점수가 더 올랐을 플랜B'를 만듭니다.

    실제 비용을 추정하지 않고, 선택 점수(delta), 라운드, event_bonus를 근거로
    다음 재도전에서 개선할 수 있는 행동 방향을 제안합니다.
    """
    suggestions: list[dict[str, Any]] = []

    route_steps = [h for h in history if h.get("is_route_step", True)]
    weak_steps = [h for h in route_steps if h.get("delta", 0) <= 3]

    for h in weak_steps[:3]:
        round_name = h.get("round", "")
        choice = h.get("choice", "")
        delta = h.get("delta", 0)
        reason = h.get("reason", "")

        if round_name == "식사":
            plan = "상대 취향을 더 직접 반영한 식당을 고르고, 너무 호불호 강한 메뉴는 피했으면 안정도가 올라갔을 가능성이 큽니다."
        elif round_name == "액티비티":
            plan = "관계 단계에 비해 부담이 적고 대화가 자연스럽게 이어지는 활동을 골랐으면 점수가 더 올랐을 가능성이 큽니다."
        elif round_name == "카페·소품샵":
            plan = "카페에서 쉬는 흐름이나 작은 취향 포인트를 챙기는 선택을 했으면 분위기 점수가 더 좋아졌을 가능성이 큽니다."
        elif round_name == "마무리":
            plan = "마지막은 이동 부담을 줄이고 배려가 드러나는 마무리를 선택했으면 엔딩 점수가 더 올라갔을 가능성이 큽니다."
        else:
            plan = "상대 페르소나와 현재 분위기에 더 맞춘 선택을 했으면 점수가 더 올라갔을 가능성이 큽니다."

        suggestions.append(
            {
                "round": round_name,
                "original_choice": choice,
                "original_effect": delta,
                "why_it_was_weak": reason,
                "plan_b": plan,
                "score_hint": "이 대안을 선택했다면 해당 라운드 점수가 더 높았을 가능성이 있습니다.",
            }
        )

    # 점수가 낮은 라운드가 없으면, 최악 선택 1개를 기준으로 부드러운 개선안을 제공합니다.
    if not suggestions and route_steps:
        worst = min(route_steps, key=lambda h: h.get("delta", 0))
        suggestions.append(
            {
                "round": worst.get("round", ""),
                "original_choice": worst.get("choice", ""),
                "original_effect": worst.get("delta", 0),
                "why_it_was_weak": worst.get("reason", ""),
                "plan_b": "전체 흐름은 괜찮았지만, 이 라운드에서 상대 취향을 한 번 더 확인하거나 이동 부담을 줄이는 선택을 했다면 완성도가 더 올라갔을 가능성이 큽니다.",
                "score_hint": "큰 실패는 아니지만, 재도전 시 더 높은 등급을 노릴 수 있는 개선 포인트입니다.",
            }
        )

    return suggestions


# ===========================================================================
# [기능 8] Tool ④ get_result — 성적표 발급
# ===========================================================================
# annotations: 조회만 하므로 readOnly. 끝난 세션에 대해선 같은 결과를 반환하므로
# idempotent.


@mcp.tool(
    name = "get_result",
    description =
    """
    Generate the final sumroute(썸루트) rehearsal report after all route rounds are complete.
    Use this only when game_over=True or all four rounds have been selected.
    The response includes the user's selected_course, a one-line route summary, final grade,
    best/worst choices, and Plan B suggestions.
    The assistant must first show the full selected course in order, then explain the result.
    Do not invent precise expected costs. Instead, focus on route flow, chemistry, risk points,
    and 'if you had chosen this Plan B, the score would have been higher' suggestions.
    """,
    annotations={
        "title": "Get SumRoute Final Report",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def get_result(
    session_id: str,
) -> dict[str, Any]:
    """
    Args:
        session_id: Session ID
    """
    session = _get_session(session_id)
    if not session.finished:
        raise ToolError("아직 데이트가 끝나지 않았습니다. 모든 라운드를 진행한 뒤 호출하세요.")

    success_rate = session.affection  # final affection (0~100) doubles as success rate

    # 등급 컷: 델타/보너스 시스템은 그대로 두고, 등급 문턱만 완화했습니다.
    if success_rate >= 92:
        grade, ending = "S", "완벽에 가까운 대성공"
    elif success_rate >= 80:
        grade, ending = "A", "성공"
    elif success_rate >= 65:
        grade, ending = "B", "꽤 괜찮음"
    elif success_rate >= 48:
        grade, ending = "C", "무난하지만 아쉬움"
    elif success_rate >= 30:
        grade, ending = "D", "위기"
    else:
        grade, ending = "F", "대참사"

    # Highest/lowest delta in history = best/worst choice.
    best = max(session.history, key=lambda h: h["delta"], default=None)
    worst = min(session.history, key=lambda h: h["delta"], default=None)

    # 사용자가 실제로 선택한 코스만 순서대로 정리합니다.
    # 이 값은 LLM 기억에 의존하지 않고 서버 기록에서 만들어지므로,
    # 마지막 결과에서 장소/선택 순서가 뒤섞이는 문제를 줄일 수 있습니다.
    selected_course = _build_selected_course(session.history)
    course_one_liner = _build_course_one_liner(selected_course)
    plan_b_suggestions = _build_plan_b_suggestions(session.history)

    return {
        "session_id": session_id,
        "selected_course": selected_course,
        "course_one_liner": course_one_liner,
        "plan_b_suggestions": plan_b_suggestions,
        "success_rate": success_rate,
        "grade": grade,
        "ending": ending,
        "best_choice": best,
        "worst_choice": worst,
        "history": session.history,
        "grade_policy": {
            "S": "92~100",
            "A": "80~91",
            "B": "65~79",
            "C": "48~64",
            "D": "30~47",
            "F": "0~29",
        },
        "random_event_summary": {
            "chance": RANDOM_EVENT_CHANCE,
            "triggered_events": list(session.used_random_events),
            "score_rule": "돌발 이벤트는 발생만으로 점수가 바뀌지 않고, 사용자의 대응 선택을 make_choice로 기록할 때 crisis_handled 또는 crisis_mishandled 보너스로 반영됩니다.",
        },
        "report_instruction": (
            "위 데이터로 최종 결과를 출력하세요. 반드시 가장 먼저 '오늘 내가 선택한 데이트 코스'를 "
            "selected_course 순서대로 요약하세요. 각 단계는 '번호. 라운드명 - 선택한 장소/행동' 형식으로 보여주세요. "
            "그 다음 '한 줄 코스'로 course_one_liner를 보여주세요. "
            "이후 '💌 데이트 성적표'를 꾸며 출력하세요. 구성: "
            "① 등급과 성공률 ② 엔딩 한 줄 평 ③ 베스트/워스트 선택과 그 이유 "
            "④ 상대방의 속마음 한 줄 ⑤ 다음 데이트 팁. "
            "예상 비용은 서버가 검증하지 않았으므로 구체적인 금액처럼 쓰지 마세요. "
            "등급 기준은 grade_policy를 참고해 설명하고, S는 95점 이상이라 쉽게 나오지 않는다는 점을 자연스럽게 반영하세요. "
            "random_event_summary에 이벤트가 있으면 어떤 돌발상황이 있었고 대응이 점수에 어떻게 반영됐는지 한 줄로 설명하세요. "
            "전체 코스가 기준 장소 반경 안에서 이어진다는 점과, 이동 부담이 적은 루트라는 점을 함께 설명하세요. "
            "대신 plan_b_suggestions를 사용해 '이렇게 했으면 점수가 더 올랐을 플랜B'를 반드시 1~3개 제시하세요. "
            "플랜B는 사용자를 비난하지 말고, 다음 재도전에서 더 높은 등급을 받을 수 있는 개선안처럼 말하세요. "
            "마지막에 '같은 상대, 다른 코스로 재도전할까요?'라고 제안하고, "
            "사용자가 원하면 동일한 페르소나로 start_date를 다시 호출하세요."
        ),
    }


# ===========================================================================
# [기능 9] 서버 실행 (엔트리포인트)
# ===========================================================================
# if __name__ == "__main__": 은 "이 파일을 직접 실행했을 때만"
# 아래 코드를 돌리라는 파이썬 관용구예요.
# (다른 파일에서 import server 할 때는 서버가 실행되지 않아, 위에서 했던
#  것처럼 테스트 코드에서 tool 함수만 불러다 쓸 수 있습니다.)

if __name__ == "__main__":
    # HTTP 모드로 실행 → 주소는 http://<호스트>:<포트>/mcp
    # PlayMCP 같은 원격 호스팅은 HTTP 방식이 필요합니다.
    # 내 컴퓨터에서 Claude Desktop 등으로 테스트하려면
    # 이 부분을 mcp.run() 으로 바꾸면 stdio 모드(기본값)로 실행돼요.
    mcp.run(
        transport="http",
        host="0.0.0.0",  # "모든 네트워크에서 접속 허용" (배포 환경용 설정)
        port=int(os.environ.get("PORT", "8000")),  # 환경변수 PORT가 없으면 8000번
    )
