"""Writes config.json (App ID, channel, uid and a fresh Agora AccessToken2 / "007" RTC token) next to join_test.html.

Needs Agora's official token builder (clone https://github.com/AgoraIO/Tools and point AGORA_TOOLS to its folder) and
AGORA_APP_ID / AGORA_APP_CERTIFICATE in integration/.env (git-ignored). Never prints the certificate or the token.
Then:  cd integration/agora_join_test ; python -m http.server 8765 --bind 127.0.0.1  and open http://127.0.0.1:8765/join_test.html
Expected last line on the page: "RESULT: PASS - connected with a 007 token". Delete config.json afterwards (it holds a 15-minute token).
"""
import json, os, pathlib, sys

tools = pathlib.Path(os.environ.get("AGORA_TOOLS", r"D:\hackathon\agora-token-test\Tools")) / "DynamicKey" / "AgoraDynamicKey" / "python3"
sys.path.insert(0, str(tools))
from src.RtcTokenBuilder2 import RtcTokenBuilder, Role_Publisher  # noqa: E402

vals = {}
for line in (pathlib.Path(__file__).resolve().parents[1] / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip().strip('"')
channel, uid = "resolvix-join-test", 4242
token = RtcTokenBuilder.build_token_with_uid(vals["AGORA_APP_ID"], vals["AGORA_APP_CERTIFICATE"], channel, uid, Role_Publisher, 900, 900)
assert token.startswith("007")
(pathlib.Path(__file__).parent / "config.json").write_text(json.dumps({"appId": vals["AGORA_APP_ID"], "channel": channel, "uid": uid, "token": token}), encoding="utf-8")
print("config.json written (007 token, 15 min); nothing secret printed")
