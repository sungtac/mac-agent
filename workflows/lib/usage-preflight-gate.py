#!/usr/bin/env python3
"""usage-preflight-gate.py <claude|codex|dual> <floor_pct>

Reads `coach --json` output from stdin, prints "PROCEED [...]" or
"SKIP: ...", exit 0 always (decision is in stdout, not exit code).

Pulled out of usage-preflight-gate.sh's inline `python3 -c '...'` into its
own file (2026-07-30) so it can be unit-tested directly with fixture JSON
on stdin — no need to stub the real `coach` binary, which the shell script
can't easily do since it hardcodes /opt/homebrew/bin at the front of PATH.
See usage-preflight-gate.test.sh.
"""
import json
import sys


def fmt_pct(windows, key):
    w = windows.get(key)
    if w and w.get("left_pct") is not None:
        return str(w["left_pct"]) + "%"
    return "N/A"  # 그 창 데이터가 아예 없음(예: codex는 구조적으로 5h창이 없음) — 생략하지 않고 명시


def check(providers, name, window_key, floor):
    """Returns (blocked, info).

    - provider itself unreadable (`ok` false/missing): (None, None) — we
      truly know nothing about it, so there's nothing to display either.
      fail-open on blocking.
    - provider readable but the specific window we need to judge
      (`window_key`) is missing/incomplete: (None, info) — still fail-open
      on the *blocking decision* (can't judge what we don't have), but the
      info line must still be built and shown (with that window as "N/A")
      rather than silently dropped. 2026-07-30 fix: the previous version
      returned (None, None) here too, so a provider that's `ok` but happens
      to be missing just the judged window (e.g. a coach hiccup returning
      claude without its 5h key) produced bare "PROCEED" with *no* info at
      all — violating "두 창 정보를 항상 다 보여주되"(사용자 확정 요구사항).
      The only place that should ever omit a provider's info entirely is
      "we don't know anything about this provider" (ok: false), not "we
      know it but can't judge one specific window".
    - normal case: (bool, info) — blocked decided purely by window_key's
      own left_pct vs floor.
    """
    p = providers.get(name) or {}
    if not p.get("ok"):
        return None, None
    windows = p.get("windows") or {}
    level = p.get("level")
    reason = p.get("reason", "")

    # 5h/7d 둘 다 항상 표시(데이터가 없는 창도 N/A로) — level/reason은 어느
    # 창의 값도 아닌 provider 전체 요약이므로 "전체상태="로 분리해서 뒤에만 붙인다.
    info = name + " 5h창 잔여 " + fmt_pct(windows, "5h") + " / 7d창 잔여 " + fmt_pct(windows, "7d")
    if level:
        info += " (전체상태=" + level + ")"
        if reason:
            info += " - " + reason

    w = windows.get(window_key)
    if not w or w.get("left_pct") is None:
        return None, info  # 차단 판정 대상 창 자체가 없으면 판단만 불가 — fail-open. 표시는 그대로.

    blocked = w["left_pct"] < floor  # window_key 자신의 잔여율만으로 판단 (전체상태는 차단과 무관, 정보로만)
    return blocked, info


def main():
    actor = sys.argv[1]
    floor = int(sys.argv[2])

    try:
        providers = json.load(sys.stdin)["providers"]
    except Exception:
        print("PROCEED (coach output unparseable - gate skipped, not enforced)")
        return

    blockers = []
    infos = []
    if actor in ("claude", "dual"):
        blocked, info = check(providers, "claude", "5h", floor)
        if info:
            infos.append(info)
            if blocked:
                blockers.append(info)
    if actor in ("codex", "dual"):
        blocked, info = check(providers, "codex", "7d", floor)
        if info:
            infos.append(info)
            if blocked:
                blockers.append(info)

    if blockers:
        # dual에서 한쪽만 막혔을 때 안 막힌 provider의 info까지 조용히
        # 사라지던 버그(2026-07-30, Codex 코드리뷰로 발견) - SKIP 사유는
        # blockers만 먼저 보여주되, 막히지 않은 provider의 info도 이어붙여서
        # "두 provider 정보를 항상 다 보여준다"를 dual일 때도 지킨다.
        others = [i for i in infos if i not in blockers]
        msg = "SKIP: " + " / ".join(blockers)
        if others:
            msg += " / " + " / ".join(others)
        print(msg)
    else:
        suffix = " - " + " / ".join(infos) if infos else ""
        print("PROCEED" + suffix)


if __name__ == "__main__":
    main()
