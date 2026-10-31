# Live protocol adapters

Copied from AidenXu-1/Lulu tag v4.3.1, commit `8ae31a76e1e135e6e016c46e270e9992c7162a51`,
`app/src/core/live_recording/{douyin,contracts,ab_sign,danmaku}.py`.
First-party license: `LULU_LICENSE.txt`. Upstream protocol references retain
`STREAMGET_LICENSE.txt` and `BILIUP_LICENSE.txt`.

Windows adaptation supplies a portable bounded `comments.py` storage adapter.
The original macOS recorder, index, XPC authority and filesystem ownership code
are not used. Network results are not assumed to be available: access/login,
offline, parsing, transport and comment states remain distinct.
