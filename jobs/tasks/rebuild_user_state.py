"""rebuild_user_state: 장기 프로필의 증분 상태(user_profile_state)를 클릭 로그에서 다시 만든다 (ADR 0033).

상태는 클릭 API가 클릭마다 갱신하는 캐시다. 이 잡은 두 경우에 쓴다.
1. 채우기: 상태 테이블이 생기기 전의 클릭, 클릭 API를 거치지 않고 들어온 클릭(시드·이관)을 반영한다.
2. 점검: `--check`는 쓰지 않고 저장된 상태가 로그와 다른 사용자만 센다(갱신 실패나 버그를 드러낸다).

사용자마다 한 트랜잭션이고 상태 행을 잠근 뒤 로그를 읽으므로, 도는 중에 들어오는 클릭과 엇갈리지 않는다.
이미 로그와 같은 상태는 다시 쓰지 않아 몇 번을 돌려도 결과가 같다.
"""
from typing import Any, Dict


def add_arguments(parser) -> None:
    parser.add_argument("--check", action="store_true", help="쓰지 않고 로그와 다른 상태만 센다")
    parser.add_argument("--user-id", type=int, action="append", default=None,
                        help="이 사용자만(여러 번 줄 수 있다). 없으면 클릭이나 상태가 있는 사용자 전부")


def rebuild_all(engine, user_ids=None, check: bool = False) -> Dict[str, Any]:
    from app.recsys import profile_store

    if user_ids is None:
        with engine.connect() as conn:
            user_ids = profile_store.users_to_rebuild(conn)
    counts = {"users": len(user_ids), "unchanged": 0, "rebuilt": 0, "mismatch": 0}
    mismatched = []
    for uid in user_ids:
        with engine.begin() as conn:
            outcome = profile_store.rebuild_user(conn, uid, write=not check)
        counts[outcome] += 1
        if outcome != "unchanged" and len(mismatched) < 20:
            mismatched.append(uid)
    counts["mode"] = "check" if check else "write"
    counts["changed_user_ids_sample"] = mismatched
    return counts


def run(ctx) -> Dict[str, Any]:
    from app.database import engine

    stats = rebuild_all(engine, user_ids=ctx.args.user_id, check=ctx.args.check)
    if ctx.args.check and stats["mismatch"]:
        ctx.warn(f"user_profile_state가 클릭 로그와 다른 사용자 {stats['mismatch']}명 "
                 f"(예: {stats['changed_user_ids_sample']}) - `rebuild_user_state`로 다시 만든다")
    return {"user_state": stats}
