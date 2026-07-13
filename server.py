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
    (키가 없으면 목업 장소 데이터로 동작)

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
    "date-simulator",  # 서버 이름 (클라이언트에 표시됨)
    instructions=(
        "리얼 데이트 시뮬레이터: 사용자가 입력한 상대방 페르소나를 당신(LLM)이 "
        "연기하는 데이트 시뮬레이션 게임입니다. 반드시 start_date로 세션을 만든 뒤, "
        "각 라운드마다 search_nearby → 선택지 제시 → make_choice 순서로 진행하고, "
        "모든 라운드가 끝나면 get_result로 성적표를 발급하세요. "
        "호감도 점수는 평소에 절대 직접 언급하지 말고, 상대방의 표정·말투·텐션으로만 "
        "표현하세요. make_choice 응답에 reveal_effect가 포함된 경우에만 "
        "점수 이펙트(💖/💔)를 노출할 수 있습니다."
    ),
)

# 환경변수에서 카카오 API 키를 읽습니다.
# os.environ.get("이름", "기본값") → 환경변수가 없으면 기본값("")을 돌려줍니다.
# 코드에 API 키를 직접 적지 않는 이유: 코드를 GitHub 등에 올릴 때 키가
# 유출되는 사고를 막기 위해서예요. (실무에서 아주 중요한 습관!)
KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY", "")
KAKAO_LOCAL_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"

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
        "search_hint": "맛집, 파스타, 초밥, 덮밥, 중식 등 식당 카테고리",
        "description": "첫 라운드. 근처 식당을 검색해 3~4개 선택지를 제시하세요.",
    },
    {
        "name": "액티비티",
        "search_hint": "공방, 전시, 방탈출, 보드게임카페 등 체험 카테고리",
        "description": "체험형 액티비티 라운드. 만난 기간을 고려해 '전에 해봤던 것' 멘트를 활용할 수 있습니다.",
    },
    {
        "name": "카페·소품샵",
        "search_hint": "카페, 소품샵, 디저트 카테고리",
        "description": "돌발 이벤트 라운드. 상대가 마음에 들어하는 물건 앞에서 선물 선택지(대놓고/몰래/안 사줌)를 반드시 발생시키세요.",
    },
    {
        "name": "마무리",
        "search_hint": "산책로, 야경 명소, 지하철역 주변",
        "description": "마지막 라운드. 배웅 방식과 헤어지기 전 마지막 한마디를 선택하게 하세요.",
    },
]

# 점수 변동이 이 값(±8) 이상으로 클 때만 💖/💔 이펙트를 노출합니다.
# 기획서 3장의 "평소엔 점수를 숨기고, 큰 변동일 때만 보여주기" 규칙이에요.
REVEAL_THRESHOLD = 8

# 특수 이벤트별 고정 보너스 점수표. (기획서의 점수 기준표를 코드로 옮긴 것)
# LLM이 make_choice를 호출할 때 event_bonus="surprise_gift"처럼
# 문자열 이름으로 지정하면, 서버가 이 표에서 점수를 찾아 더해줍니다.
EVENT_BONUS: dict[str, int] = {
    "surprise_gift": 5,   # 몰래 사서 서프라이즈
    "open_gift": 3,       # 대놓고 사준다
    "no_gift": 0,         # 안 사준다
    "crisis_handled": 5,  # 돌발 이벤트 잘 대처
    "repeat_date": -3,    # 같은 데이트 반복 (기간 대비)
    "none": 0,            # 특수 이벤트 없음 (기본값)
}

# 돌발 랜덤 이벤트 목록. 라운드가 넘어갈 때 아래 확률로 하나가 뽑힙니다.
RANDOM_EVENTS: list[str] = [
    "갑자기 비가 쏟아지기 시작한다. (우산은 하나뿐이다…!)",
    "가려던 곳 앞에 웨이팅이 1시간이라고 한다.",
    "상대방이 새 신발 때문에 발이 아파 보인다.",
    "상대방의 옛 친구를 우연히 마주쳤다. 분위기가 미묘해진다.",
    "상대방 휴대폰 배터리가 3% 남았다며 초조해한다.",
]
RANDOM_EVENT_CHANCE = 0.35  # 35% 확률

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


# ===========================================================================
# [기능 4] 장소 검색 (카카오 로컬 API + 목업 폴백)
# ===========================================================================
# "이 게임의 배경이 진짜 우리 동네"가 되게 해주는 핵심 기능.
# 카카오 로컬 API에 검색어를 보내면 실제 가게 목록을 받아옵니다.

# 목업(mock) 데이터: API 키가 없거나 API가 실패했을 때 대신 쓰는 가짜 장소들.
# 덕분에 키 없이도 게임 전체 흐름을 테스트할 수 있어요.
MOCK_PLACES: dict[str, list[dict[str, str]]] = {
    "식사": [
        {"place_name": "포모도로 파스타", "category_name": "음식점 > 양식", "road_address_name": "중앙로 12"},
        {"place_name": "규동집 온", "category_name": "음식점 > 일식", "road_address_name": "중앙로 34"},
        {"place_name": "홍복 중화요리", "category_name": "음식점 > 중식", "road_address_name": "시장길 5"},
        {"place_name": "스시 마루", "category_name": "음식점 > 일식 > 초밥", "road_address_name": "역전로 8"},
    ],
    "액티비티": [
        {"place_name": "실버링 반지공방", "category_name": "공방 > 금속공예", "road_address_name": "공방골목 3"},
        {"place_name": "흙과 물레 도자기공방", "category_name": "공방 > 도예", "road_address_name": "공방골목 7"},
        {"place_name": "포근포근 러그공방", "category_name": "공방 > 텍스타일", "road_address_name": "공방골목 11"},
    ],
    "카페·소품샵": [
        {"place_name": "달빛 소품샵", "category_name": "소품샵", "road_address_name": "골목길 2"},
        {"place_name": "카페 모리", "category_name": "카페 > 디저트", "road_address_name": "골목길 9"},
        {"place_name": "종이와 연필 문구점", "category_name": "소품샵 > 문구", "road_address_name": "골목길 15"},
    ],
    "마무리": [
        {"place_name": "강변 산책로", "category_name": "산책로", "road_address_name": "강변북길"},
        {"place_name": "달맞이 전망대", "category_name": "야경 명소", "road_address_name": "언덕길 1"},
        {"place_name": "지하철역 2번 출구", "category_name": "교통", "road_address_name": "역전로 1"},
    ],
}


# "async def"는 비동기 함수라는 뜻이에요.
# 외부 API 응답을 기다리는 동안(수십 ms~수 초) 서버가 멈춰 있지 않고
# 다른 요청을 처리할 수 있게 해줍니다. 기다리는 지점마다 await를 붙여요.
async def _search_places(query: str, round_name: str, size: int = 5) -> list[dict[str, str]]:
    """카카오 로컬 API로 장소 검색. API 키가 없거나 실패하면 목업 데이터 반환."""
    if KAKAO_REST_API_KEY:  # 빈 문자열("")은 False로 취급 → 키가 있을 때만 실행
        try:
            # "async with"는 사용이 끝나면 연결을 자동으로 정리해주는 문법
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(  # await = "응답이 올 때까지 여기서 대기"
                    KAKAO_LOCAL_URL,
                    params={"query": query, "size": size},  # URL 뒤에 붙는 검색 조건
                    # 카카오 API는 인증 헤더에 "KakaoAK <키>" 형식을 요구합니다
                    headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"},
                )
                resp.raise_for_status()  # 응답이 에러(4xx/5xx)면 예외를 발생시킴
                documents = resp.json().get("documents", [])  # JSON에서 장소 목록 꺼내기
                if documents:
                    # 카카오가 주는 필드는 매우 많아서,
                    # 게임에 필요한 3가지(이름/카테고리/주소)만 골라 담습니다.
                    return [
                        {
                            "place_name": d.get("place_name", ""),
                            "category_name": d.get("category_name", ""),
                            # 도로명 주소가 없으면 지번 주소를 대신 사용 ("or"의 활용)
                            "road_address_name": d.get("road_address_name")
                            or d.get("address_name", ""),
                        }
                        for d in documents
                    ]
        except httpx.HTTPError:
            # 네트워크 오류가 나도 게임이 죽지 않도록 조용히 넘어가고(pass),
            # 아래의 목업 데이터로 대신 응답합니다. ("폴백" 패턴)
            pass
    # 여기 도달하는 경우: ① API 키 없음 ② API 실패 ③ 검색 결과 0건
    return MOCK_PLACES.get(round_name, MOCK_PLACES["식사"])[:size]


# ===========================================================================
# [기능 5] Tool ① start_date — 게임 시작
# ===========================================================================
# annotations: PlayMCP 가이드 필수 property. 상태를 새로 만드므로 destructive.


@mcp.tool(
    name = "start_date",
    description = 
    """
    Starts a new date simulation session for 리얼 데이트 시뮬레이터(Date Simulator).
    Returns a session_id and the first round info.
    Follow roleplay_instruction in the response to act out the partner persona.
    """,
    annotations = {
        "title": "Start Date Simulation",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def start_date(
    partner_gender: str,
    meeting_place: str,
    persona: str,
    relationship_stage: str,
) -> dict[str, Any]:
    """
    Args:
        partner_gender: Partner's gender ("남" / "여" / "기타")
        meeting_place: Meeting spot (e.g. "홍대입구역") — anchor point for place search
        persona: Partner's personality/taste as free text
            (e.g. "shy at first, dislikes spicy food, loves cute small items")
        relationship_stage: How long the couple has been together
            ("썸" / "1~3개월" / "1년차" / "장기연애")
    """
    # Create a new game session.
    session = Session(
        session_id=uuid.uuid4().hex[:12],  # short random ID
        partner_gender=partner_gender,
        meeting_place=meeting_place,
        persona=persona,
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
            f"페르소나: '{persona}'. 만난 기간: {relationship_stage}. "
            f"페르소나에 맞는 말투와 텐션을 처음부터 끝까지 일관되게 유지하세요. "
            f"먼저 {meeting_place}에서 만나는 장면을 짧게 연출하고 "
            f"(만난 기간에 어울리는 첫 인사 톤으로), search_nearby로 근처 식당을 "
            f"검색해 1라운드 선택지를 제시하세요. "
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
    Searches nearby real places for the current round in 리얼 데이트 시뮬레이터(Date Simulator).
    Pick 3~4 places to present as choices. 
    Design rule: never make a perfect-match choice always available — sometimes leave only imperfect options so the user picks 'the best of the worst'
    """,
    annotations={
        "title": "Search Nearby Places",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def search_nearby(
    session_id: str,
    query: str,
) -> dict[str, Any]:
    """
    Args:
        session_id: Session ID issued by start_date
        query: Search keywords. Always include the meeting place
            (e.g. "홍대입구역 파스타", "홍대입구역 공방"). Refer to the current
            round's search_hint category.
    """
    session = _get_session(session_id)
    if session.finished:
        raise ToolError("이미 종료된 세션입니다. get_result를 호출하거나 새 세션을 시작하세요.")

    round_info = ROUNDS[session.round_index]
    places = await _search_places(query, round_info["name"])

    return {
        "session_id": session_id,
        "current_round": {"number": session.round_index + 1, **round_info},
        "places": places,
        "presentation_instruction": (
            "위 장소 중 3~4곳을 번호 붙은 선택지로 제시하세요. "
            "상대방(페르소나)이 선택지를 보고 가볍게 한마디 하게 해도 좋습니다. "
            "사용자가 고르면 make_choice를 호출하세요. "
            "라운드 사이에 가끔 멘트 선택지(대화 선택)도 끼워 넣으세요. "
            "예: 상대가 '나 오늘 좀 피곤해 보이지…?'라고 물으면 "
            "① 아니? 예쁜데? ② 어 좀 피곤해 보여 ③ 그럼 카페 가서 쉴까? — "
            "이 경우에도 결과는 make_choice로 기록합니다."
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
    Records the user's choice and updates affection in 리얼 데이트 시뮬레이터(Date Simulator).
    Before calling, judge how well the choice matches the persona.
    """,
    annotations={
        "title": "Record Date Choice",
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
        taste_match_delta: Your taste-match judgment score (-10 ~ +10).
            Perfect match +5~+10, neutral -2~+4, mismatch -5~-10.
            Correct answer on a dialogue choice: +3~+5.
        judge_reason: One-line reasoning (used for best/worst pick in the final report)
        event_bonus: Set only when applicable —
            "surprise_gift"(+5) / "open_gift"(+3) / "no_gift"(0) /
            "crisis_handled"(+5) / "repeat_date"(-3) / "none"
        advance_round: True (default) if this choice ends the current round.
            False for a mid-round choice (dialogue choice, gift event, etc.).
    """
    session = _get_session(session_id)
    if session.finished:
        raise ToolError("이미 종료된 세션입니다.")

    if event_bonus not in EVENT_BONUS:
        raise ToolError(f"event_bonus는 {list(EVENT_BONUS)} 중 하나여야 합니다.")

    # Clamp LLM's score into the allowed range, then apply event bonus.
    taste_match_delta = _clamp(taste_match_delta, -10, 10)
    total_delta = taste_match_delta + EVENT_BONUS[event_bonus]
    session.affection = _clamp(session.affection + total_delta, 0, 100)

    round_info = ROUNDS[session.round_index]
    session.history.append(
        {
            "round": round_info["name"],
            "choice": choice_description,
            "delta": total_delta,
            "reason": judge_reason,
            "event_bonus": event_bonus,
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
            random_event = random.choice(RANDOM_EVENTS)

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
        "next_round": next_round,
        "game_over": game_over,
        # Instruction text is assembled conditionally below.
        "reaction_instruction": (
            "mood_hint를 참고해 상대방의 리액션을 페르소나 말투로 연기하세요. "
            "점수·호감도 수치는 절대 언급 금지. "
            + (f"단, 이번엔 '{reveal_effect}' 이펙트를 짧게 노출해도 됩니다. " if reveal_effect else "")
            + ("게임이 끝났습니다. 헤어지는 장면을 연출한 뒤 get_result를 호출해 성적표를 발급하세요."
               if game_over
               else "리액션 후 다음 라운드로 자연스럽게 넘어가세요.")
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


# ===========================================================================
# [기능 8] Tool ④ get_result — 성적표 발급
# ===========================================================================
# annotations: 조회만 하므로 readOnly. 끝난 세션에 대해선 같은 결과를 반환하므로
# idempotent.


@mcp.tool(
    name = "get_result",
    description =
    """
    Issues the final report card after a date ends in 리얼 데이트 시뮬레이터(Date Simulator).
    Call only after all rounds are complete.
    """,
    annotations={
        "title": "Get Date Result",
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

    if success_rate >= 90:
        grade, ending = "S", "대성공"
    elif success_rate >= 75:
        grade, ending = "A", "성공"
    elif success_rate >= 60:
        grade, ending = "B", "무난"
    elif success_rate >= 40:
        grade, ending = "C", "아쉬움"
    elif success_rate >= 20:
        grade, ending = "D", "위기"
    else:
        grade, ending = "F", "대참사"

    # Highest/lowest delta in history = best/worst choice.
    best = max(session.history, key=lambda h: h["delta"], default=None)
    worst = min(session.history, key=lambda h: h["delta"], default=None)

    return {
        "session_id": session_id,
        "success_rate": success_rate,
        "grade": grade,
        "ending": ending,
        "best_choice": best,
        "worst_choice": worst,
        "history": session.history,
        "report_instruction": (
            "위 데이터로 '💌 데이트 성적표'를 꾸며 출력하세요. 구성: "
            "① 등급과 성공률 ② 엔딩 한 줄 평 (예: '다음 데이트는 꼭 성공하길…!!') "
            "③ 베스트/워스트 선택과 그 이유 ④ 상대방의 속마음 한 줄 "
            "(페르소나 말투로, 사용자에게 들키지 않았던 진짜 기분) "
            "⑤ 다음 데이트를 위한 팁 1~2개. "
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
