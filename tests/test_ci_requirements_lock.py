"""CI 잠금 파일(requirements-ci.txt)이 원본 고정 파일과 어긋나지 않는지 대조한다.

CI는 파이썬 패키지를 잠금 파일 하나로만 설치한다(.github/workflows/ci.yml). 그 파일의 값 중 둘은 다른 곳이 원본이다:
- 추천 평가 리포트를 만든 버전: evaluation/requirements.txt
- API 이미지가 싣는 버전: backend/requirements.txt (fastapi·sqlalchemy·sqlmodel 등)
원본을 고치고 `make ci-lock`을 잊으면 CI가 리포트·이미지와 다른 버전에서 돈다 - 그 사고(PR #26)를 여기서 잡는다.
표준 라이브러리만 쓰고 파일만 읽는다.
"""
import re
import shlex
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# evaluation/requirements.txt의 fastapi는 라벨링 UI용이다. CI의 라우터 테스트는 API 이미지와 같은 fastapi에서 돌아야 하므로
# 이 하나만 backend/requirements.txt 값이 이긴다(requirements-ci.in 머리말).
REPORT_PIN_EXCEPTIONS = {"fastapi"}
API_IMAGE_PINS = ("fastapi", "python-jose", "passlib", "bcrypt", "sqlalchemy", "sqlmodel")

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")
_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(path: str) -> dict:
    """requirements 파일에서 `이름==버전` 줄을 {정규화한 이름: {버전}}으로 읽는다(환경 마커가 달린 줄도 센다)."""
    found: dict = {}
    for line in (REPO / path).read_text(encoding="utf-8").splitlines():
        m = _PIN.match(line)  # `# via` 주석 줄은 들여쓰기가 있어 걸리지 않는다
        if m:
            found.setdefault(_norm(m.group(1)), set()).add(m.group(2))
    return found


def _top_level(path: str) -> dict:
    """.in 파일의 최상위 요구사항을 {정규화한 이름: 버전 또는 None}으로 읽는다."""
    found: dict = {}
    for raw in (REPO / path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _PIN.match(line)
        if m:
            found[_norm(m.group(1))] = m.group(2)
        else:
            n = _NAME.match(line)
            assert n, f"requirements-ci.in 줄을 읽지 못했습니다: {raw!r}"
            found[_norm(n.group(1))] = None
    return found


def test_every_top_level_requirement_is_in_the_lock():
    lock = _pins("requirements-ci.txt")
    for name, version in _top_level("requirements-ci.in").items():
        assert name in lock, f"{name}이 requirements-ci.txt에 없습니다 - `make ci-lock`을 다시 실행하세요"
        if version is not None:
            assert lock[name] == {version}, (
                f"{name}: requirements-ci.in은 =={version}인데 잠금 파일은 {sorted(lock[name])}입니다 - `make ci-lock`"
            )


def test_lock_matches_the_versions_the_reports_were_made_with():
    lock = _pins("requirements-ci.txt")
    for name, versions in _pins("evaluation/requirements.txt").items():
        if name in REPORT_PIN_EXCEPTIONS:
            continue
        assert lock.get(name) == versions, (
            f"{name}: evaluation/requirements.txt는 {sorted(versions)}, 잠금 파일은 {sorted(lock.get(name, []))} "
            "- 리포트를 만든 버전과 CI가 달라집니다. requirements-ci.in을 고치고 `make ci-lock`"
        )


def test_lock_matches_the_versions_the_api_image_ships():
    lock = _pins("requirements-ci.txt")
    image = _pins("backend/requirements.txt")
    for name in API_IMAGE_PINS:
        assert name in image, f"backend/requirements.txt에 {name}이 없습니다(대조 목록을 고치세요)"
        assert lock.get(name) == image[name], (
            f"{name}: backend/requirements.txt(API 이미지)는 {sorted(image[name])}, "
            f"잠금 파일은 {sorted(lock.get(name, []))}"
        )


# 워크플로의 파이썬 설치 명령에서 허용하는 조각. 패키지 이름을 받는 설치(`uv pip install "fastapi==..."`)는 하나도 허용하지 않는다:
# 그런 스텝이 잠금 파일 밖에서 핀을 덮어쓰면 CI가 리포트·이미지와 다른 버전에서 돈다(PR #26).
LOCK_FILE = "requirements-ci.txt"
_INSTALL = re.compile(r"\b(?:pip\s+(?:install|sync)|uv\s+(?:add|sync))\b")


def _ci_commands(ci: str) -> list:
    """워크플로 텍스트에서 주석 줄을 빼고 줄 끝 `\\`로 이어진 명령을 한 줄로 합쳐 줄 목록으로 돌려준다."""
    ci = re.sub(r"(?m)^\s*#.*\n", "", ci)
    ci = re.sub(r"\\\n\s*", " ", ci)
    return ci.splitlines()


def _install_violations(ci: str) -> list:
    """ci.yml에서 잠금 파일 밖으로 나가는 파이썬 설치를 찾아 이유와 함께 돌려준다(없으면 빈 목록).

    허용: `-r requirements-ci.txt`, `--no-deps -e <ai_workspace 아래 경로>`. `--python <경로>`는 옵션이라 허용.
    그 밖의 모든 `pip install|sync`, `uv add|sync`와, 잠금 파일 설치 없이 pytest를 돌리는 잡은 위반이다.
    """
    problems = []
    for job, lines in _jobs(ci).items():
        lock_installs = 0
        seen_pytest = False
        for line in _ci_commands("\n".join(lines)):
            if "pytest" in line:
                seen_pytest = True
            m = _INSTALL.search(line)
            if not m:
                continue
            tokens = shlex.split(line[m.start():])
            if tokens[0] == "uv":  # `uv add`, `uv sync`는 잠금 파일을 거치지 않는다
                problems.append(f"{job}: 잠금 파일 밖의 설치: {line.strip()}")
                continue
            rest, i, locks, editables, no_deps, foreign = tokens[2:], 0, [], [], False, []
            while i < len(rest):
                tok = rest[i]
                if tok == "--python":
                    i += 2
                elif tok == "-r" and i + 1 < len(rest):
                    locks.append(rest[i + 1])
                    i += 2
                elif tok == "-e" and i + 1 < len(rest):
                    editables.append(rest[i + 1])
                    i += 2
                elif tok == "--no-deps":
                    no_deps = True
                    i += 1
                else:
                    foreign.append(tok)
                    i += 1
            if foreign:
                problems.append(f"{job}: 잠금 파일 밖에서 패키지를 설치합니다 {foreign}: {line.strip()}")
            elif locks == [LOCK_FILE] and not editables:
                lock_installs += 1
                if seen_pytest:
                    problems.append(f"{job}: pytest 뒤에 잠금 파일을 설치합니다: {line.strip()}")
            elif editables and not locks and no_deps and all(e.startswith("ai_workspace") for e in editables):
                pass
            else:
                problems.append(f"{job}: 허용하지 않는 설치 형태: {line.strip()}")
        if seen_pytest and lock_installs != 1:
            problems.append(f"{job}: pytest를 돌리는데 {LOCK_FILE} 설치가 {lock_installs}번입니다(정확히 1번이어야 합니다)")
    return problems


def _jobs(ci: str) -> dict:
    """`jobs:` 아래 들여쓰기 2칸 키를 잡 이름으로 보고 {잡 이름: 그 잡의 줄 목록}으로 나눈다."""
    body = ci.split("\njobs:\n", 1)[1]
    jobs: dict = {}
    name = None
    for line in body.splitlines():
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            name = m.group(1)
            jobs[name] = []
        elif name is not None:
            jobs[name].append(line)
    return jobs


def _ci_text() -> str:
    return (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def test_ci_workflow_installs_python_packages_only_from_the_lock():
    ci = _ci_text()
    assert _install_violations(ci) == []
    # 검사기가 비어 있어서 통과하는 일이 없게, 파이썬 테스트를 돌리는 잡(단위·통합)이 실제로 잡혀 있는지 본다.
    pytest_jobs = [j for j, lines in _jobs(ci).items() if any("pytest" in ln for ln in lines)]
    assert {"test", "integration-test"} <= set(pytest_jobs)


def test_install_check_catches_a_step_that_overrides_pins_outside_the_lock():
    # 리뷰에서 재현한 경우: 잠금 파일 설치 뒤에 패키지를 이름으로 설치하는 스텝(한 줄, 따옴표, 줄 이음 모두).
    ci = _ci_text()
    marker = "      - name: Run tests (excluding integration)"
    assert marker in ci
    override = (
        "      - name: Override pins\n"
        '        run: uv pip install --python .venv/bin/python "fastapi==0.141.1" "pandas==2.2.0" kiwipiepy\n\n'
    )
    assert _install_violations(ci.replace(marker, override + marker, 1)), "패키지 이름 설치를 놓쳤습니다"
    continued = override.replace(
        '--python .venv/bin/python "fastapi==0.141.1"', "--python .venv/bin/python \\\n            \"fastapi==0.141.1\""
    )
    assert _install_violations(ci.replace(marker, continued + marker, 1)), "줄 이음 설치를 놓쳤습니다"
    # 잠금 파일 대신 다른 requirements 파일, 또는 pip/sync 형태
    for swapped in (
        "uv pip install --python .venv/bin/python -r backend/requirements.txt",
        "python -m pip install pandas",
        "uv sync",
    ):
        step = f"      - name: Other install\n        run: {swapped}\n\n"
        assert _install_violations(ci.replace(marker, step + marker, 1)), f"{swapped}를 놓쳤습니다"
    # 단위 테스트 잡이 잠금 파일 설치를 잃으면(아니면 두 번 하면) 걸린다.
    lock_step = "uv pip install --python .venv/bin/python -r requirements-ci.txt"
    assert lock_step in ci
    assert _install_violations(ci.replace(lock_step, "true", 1)), "잠금 파일 설치가 빠진 잡을 놓쳤습니다"
