# Synthetic playback fixture

`sample.mp4` is deliberately retained for offline Video.js playback, seeking and
HTTP Range regression tests. It is an eight-second, 320×180 H.264 procedural
FFmpeg test pattern, not downloaded Telegram or advertising media. It has no
audio, camera footage, people, credentials, location or personal metadata.
Container metadata contains only MP4 compatibility brands and the encoder name.
Size: 79,760 bytes. This generated fixture is distributed under the project's
MIT license, as declared in `pyproject.toml`; it contains no third-party recording.

An equivalent fixture can be generated with FFmpeg (encoder versions/settings may
produce different bytes):

```sh
ffmpeg -f lavfi -i testsrc=size=320x180:rate=24 -t 8 -an \
  -c:v libx264 -pix_fmt yuv420p -movflags +faststart sample.mp4
```

FFmpeg is a developer-only regeneration dependency; installation and normal
application deployment do not require running this command. Tests never send this
fixture to an advertising network. The entire `tests/` directory is excluded from
production image contexts.
