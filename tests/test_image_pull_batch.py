"""Shape of the batched image pull in roles/build_image (M111-01, audit P1).

The two serial `docker pull` loops (one per service, one per accessory) are
one shell task that fans the whole list out with `xargs -P 4`. This pins the
properties that made the merge safe:

- exactly one pull task, and no `loop:` on it;
- retry semantics preserved (`retries: 3`, `delay: 10`, `until` on rc);
- `changed` is gated on "Downloaded newer image" — the old
  `changed_when: true` reported a change on every deploy;
- the task is skipped when the image list is empty (`printf` with no operands
  still emits one empty line, which `xargs` would hand to `docker pull`);
- image names go through the `quote` filter and `printf`, never into the
  shell bare;
- no short-circuiting reader (`grep -q`) after the pipe. `set -o pipefail` is
  the house pattern for a piped shell task and ansible-lint's production
  profile requires it, but paired with a reader that exits early it turns a
  successful command into rc 141 — that regression shipped once. `xargs` reads
  its input to the end, so the pipeline rc is the pull result.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_TASKS = Path(__file__).parent.parent / "roles" / "build_image" / "tasks" / "main.yml"


def _pull_task() -> dict:
    tasks = yaml.safe_load(_TASKS.read_text())
    pulls = [t for t in tasks if "docker pull" in str(t.get("ansible.builtin.shell", ""))
             or "docker pull" in str(t.get("ansible.builtin.command", ""))]
    assert len(pulls) == 1, f"expected exactly one pull task, got {len(pulls)}"
    return pulls[0]


def test_single_shell_task_replaces_both_loops():
    task = _pull_task()
    assert "ansible.builtin.shell" in task
    assert "loop" not in task
    assert _TASKS.read_text().count("docker pull") == 1


def test_pull_runs_in_parallel_through_xargs():
    cmd = _pull_task()["ansible.builtin.shell"]["cmd"]
    assert "xargs -r -d '\\n' -P 4 -n 1 docker pull" in cmd
    assert "printf '%s\\n'" in cmd
    assert "map('quote')" in cmd


def test_xargs_does_not_re_parse_the_quoting():
    """`| quote` protects printf; xargs' own quote syntax would undo it.

    Default xargs input syntax honours quotes and backslashes, so an image
    name containing either was split into two arguments after printf had
    already written it as one line. `-d '\n'` turns every line into exactly
    one argument, whatever is inside it.
    """
    cmd = _pull_task()["ansible.builtin.shell"]["cmd"]
    xargs = cmd.strip().split("|")[-1]
    assert "-d '\\n'" in xargs, "xargs must take one argument per line"
    assert "-r " in xargs, "an empty list must not invoke the pull at all"


def test_retry_semantics_preserved():
    task = _pull_task()
    assert task["retries"] == 3
    assert task["delay"] == 10
    assert "rc == 0" in task["until"]


def test_changed_is_gated_on_downloaded_newer_image():
    changed_when = _pull_task()["changed_when"]
    assert changed_when is not True
    assert "Downloaded newer image" in changed_when


def test_skipped_when_image_list_is_empty():
    when = _pull_task()["when"]
    assert "_pull_images" in when
    assert "length > 0" in when


def test_no_short_circuiting_reader_after_the_pipe():
    shell = _pull_task()["ansible.builtin.shell"]
    cmd = shell["cmd"]
    assert "grep -q" not in cmd
    assert "head -" not in cmd
    # xargs must be the last stage, so nothing can close the pipe early.
    assert cmd.strip().split("|")[-1].strip().startswith("xargs")
    if "pipefail" in cmd:
        # pipefail is a bashism; /bin/sh is dash on Debian/Ubuntu targets.
        assert shell["executable"] == "/bin/bash"
