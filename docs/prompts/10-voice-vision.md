# Phase 10 — Voice and Vision

Add one capability at a time, starting with push-to-talk rather than always-on wake word. Benchmark candidate local/free tools and clearly document data flow, license and quotas before selection. Do not download multi-GB models until a named model is approved after RAM/latency estimation. Hosted services must not receive sensitive voice/images by default.

Add STT input, TTS output, visible listening state, mute/cancel, timeouts and image/screenshot/document understanding behind adapters. Keep model weights out of Git. If local capability is slow, document authorized free-provider limitations; never silently send data externally.

Acceptance: measure latency/CPU/RAM/failure behavior; user can stop listening; no background recording; provider failures tested; text operation survives voice failure. Update status and benchmark docs.
