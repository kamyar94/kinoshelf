"""Hook protocol: command, folder and http kinds."""

import json
import sys

from kinoshelf.hooks import run_command, run_folder, run_http
from kinoshelf.models import LookupError_
from kinoshelf.paths import PathMap

BODY = {"protocol": 1, "stage": "subtitles", "item": {"id": 7, "file": "/x/a.mkv"}, "settings": {}, "options": {}}


def test_command_hook_answers_last_json_line(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text("import json,sys\njob=json.load(sys.stdin)\nprint('working...')\n"
                      "print(json.dumps({'status':'done','message':'ok '+job['stage'],'files':['/x/a.fa.srt']}))\n")
    res = run_command({"command": [sys.executable, str(script)]}, BODY, tmp_path)
    assert res == {"status": "done", "message": "ok subtitles", "files": ["/x/a.fa.srt"]}


def test_command_hook_without_answer_fails(tmp_path):
    script = tmp_path / "bad.py"
    script.write_text("import sys\nsys.stderr.write('boom')\nsys.exit(2)\n")
    res = run_command({"command": [sys.executable, str(script)]}, BODY, tmp_path)
    assert res["status"] == "failed" and "boom" in res["message"]


def test_folder_hook_round_trip(tmp_path):
    pm = PathMap([{"shown": "X:\\", "container": str(tmp_path)}])
    hook = {"kind": "folder", "folder": "X:\\hookjobs"}
    first = run_folder(hook, BODY, pm)
    assert first["status"] == "pending"
    job = tmp_path / "hookjobs" / "7-subtitles.job.json"
    assert json.loads(job.read_text(encoding="utf-8"))["item"]["id"] == 7
    (tmp_path / "hookjobs" / "7-subtitles.result.json").write_text(
        '{"status": "done", "message": "Persian found"}', encoding="utf-8-sig")
    second = run_folder(hook, BODY, pm)
    assert second == {"status": "done", "message": "Persian found"}
    assert not job.exists() and len(list((tmp_path / "hookjobs" / "done").iterdir())) == 2


class _Net:
    def __init__(self, answer=None, fail=False):
        self.answer, self.fail, self.sent = answer, fail, None

    def post_json(self, url, body, headers=None, timeout=None):
        if self.fail:
            raise LookupError_("down")
        self.sent = (url, body, headers)
        return self.answer


def test_http_hook():
    net = _Net({"status": "skipped", "message": "not needed"})
    res = run_http({"url": "http://svc:9000/hook", "headers": {"X-Token": "t"}}, BODY, net)
    assert res["status"] == "skipped" and net.sent[2] == {"X-Token": "t"}
    res = run_http({"url": "http://svc:9000/hook"}, BODY, _Net(fail=True))
    assert res["status"] == "pending"
    res = run_http({"url": "http://svc:9000/hook"}, BODY, _Net({"status": "maybe"}))
    assert res["status"] == "failed"
