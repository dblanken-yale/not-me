# Changelog

## [0.3.0](https://github.com/dblanken-yale/not-me/compare/not-me-v0.2.0...not-me-v0.3.0) (2026-10-23)


### Features

* **review:** review.block_on chooses which severities request changes ([#65](https://github.com/dblanken-yale/not-me/issues/65)) ([6f2b37e](https://github.com/dblanken-yale/not-me/commit/6f2b37e20ade6fbf81e98e4d1c4b70291805aeec))


### Bug Fixes

* **guard:** no directory change with writes, proven reads, and a user-granted merge ([#64](https://github.com/dblanken-yale/not-me/issues/64)) ([a8b195c](https://github.com/dblanken-yale/not-me/commit/a8b195c5b68203ca5e5302fb2533aa093ac824fd))

## [0.2.0](https://github.com/dblanken-yale/not-me/compare/not-me-v0.1.0...not-me-v0.2.0) (2026-10-22)


### Features

* add /not-me:mode with a token-signed live switch ([#55](https://github.com/dblanken-yale/not-me/issues/55)) ([d870419](https://github.com/dblanken-yale/not-me/commit/d8704195b6e827703f73a00d5a82df36b4fab4bd))
* CLI commands the skills asked for ([#16](https://github.com/dblanken-yale/not-me/issues/16)) ([332f34f](https://github.com/dblanken-yale/not-me/commit/332f34f7151c4f3ca5a8056f12078314291ab95b))
* feedback bundling with author trust filtering ([#7](https://github.com/dblanken-yale/not-me/issues/7)) ([816cd00](https://github.com/dblanken-yale/not-me/commit/816cd008fbde171c4aa29e4a9b2b6b9a4a46176b))
* gh and git client layer with write gate and dry-run ledger ([#3](https://github.com/dblanken-yale/not-me/issues/3)) ([8b1fd83](https://github.com/dblanken-yale/not-me/commit/8b1fd83b001998ddf615068c0041f1e879e727a3))
* grade a skill run from its Claude Code transcript ([#33](https://github.com/dblanken-yale/not-me/issues/33)) ([1665a40](https://github.com/dblanken-yale/not-me/commit/1665a4001133651f258cd80f90d49707ccbd5c55))
* layered configuration core for the notme package ([#1](https://github.com/dblanken-yale/not-me/issues/1)) ([133d1df](https://github.com/dblanken-yale/not-me/commit/133d1df5128d3d5e9bec7357b6452a5c58a63545))
* notme command-line entry point with a hardened trust anchor ([#14](https://github.com/dblanken-yale/not-me/issues/14)) ([ede1225](https://github.com/dblanken-yale/not-me/commit/ede12256b97fb93e1c34537d2a11176fce29a730))
* PreToolUse guard with four rule groups and a cross-platform launcher ([#5](https://github.com/dblanken-yale/not-me/issues/5)) ([54273ce](https://github.com/dblanken-yale/not-me/commit/54273cef055a67ae0db4a667aefbb625edc73cc3))
* router, status, doctor, config and allow-repo skills ([#22](https://github.com/dblanken-yale/not-me/issues/22)) ([cef8603](https://github.com/dblanken-yale/not-me/commit/cef86032c15faa39f240fe92b1f114d5fe3062b0))
* run log and active-run marker ([#2](https://github.com/dblanken-yale/not-me/issues/2)) ([5a6b1f5](https://github.com/dblanken-yale/not-me/commit/5a6b1f58f4700a1c873e1fa415195dbdcf1bd18d))
* the new skill picks up an issue and opens a PR ([#18](https://github.com/dblanken-yale/not-me/issues/18)) ([67ca680](https://github.com/dblanken-yale/not-me/commit/67ca680d11dc16f1fa6e3dde09d1a5d25eeb3491))
* the prove skill runs a PR's testing steps on its deploy target ([#21](https://github.com/dblanken-yale/not-me/issues/21)) ([54593d5](https://github.com/dblanken-yale/not-me/commit/54593d5fe8eb86887995c270237d156c457156a2))
* the review skill approves or sends back PRs under review ([#20](https://github.com/dblanken-yale/not-me/issues/20)) ([7f1713c](https://github.com/dblanken-yale/not-me/commit/7f1713c1ebaf9ef9f86bb9fa55f64768efc8e722))
* the setup skill writes the config docs from one interview ([#17](https://github.com/dblanken-yale/not-me/issues/17)) ([41f7a15](https://github.com/dblanken-yale/not-me/commit/41f7a15cdf8a6f909c68018a2b7248c0d8691ed2))
* the update skill reworks a PR that needs work ([#19](https://github.com/dblanken-yale/not-me/issues/19)) ([ff8438c](https://github.com/dblanken-yale/not-me/commit/ff8438c821d814fc170ae07a7559d7e0dcebe262))
* ticket and retro skills ([#23](https://github.com/dblanken-yale/not-me/issues/23)) ([0b33163](https://github.com/dblanken-yale/not-me/commit/0b33163833d0125e7f3f9afeeba3979ef07829b6))
* work queues, claims and label state transitions ([#6](https://github.com/dblanken-yale/not-me/issues/6)) ([75ad3e9](https://github.com/dblanken-yale/not-me/commit/75ad3e9c0f6e463b5fbddd53243fd0e813b2e41b))
* write-back layer for comments, reviews, PR bodies and follow-ups ([#8](https://github.com/dblanken-yale/not-me/issues/8)) ([55ee264](https://github.com/dblanken-yale/not-me/commit/55ee26422b9ef3759bf9604333b0bf00efdbefdc))


### Bug Fixes

* human-only commands need a real person, not a forged terminal ([#26](https://github.com/dblanken-yale/not-me/issues/26)) ([7c348dd](https://github.com/dblanken-yale/not-me/commit/7c348dd9c9444d62df017a6251b2274f5920507e))
* parked PRs always carry needs human; describe the fix-loop cap outcome ([#24](https://github.com/dblanken-yale/not-me/issues/24)) ([8d7f480](https://github.com/dblanken-yale/not-me/commit/8d7f4808e69708e183116121c0515954d914bafb))
* pick-issue accepts issues assigned to the operator ([#36](https://github.com/dblanken-yale/not-me/issues/36)) ([aa11745](https://github.com/dblanken-yale/not-me/commit/aa117451dd80b6c357dfde44f946b805e0304fbc))
* qualify the closing reference and comment on the issue when a PR opens ([#37](https://github.com/dblanken-yale/not-me/issues/37)) ([69854d0](https://github.com/dblanken-yale/not-me/commit/69854d0442f642095ce31a1949d08340820bbe69))
* **skills:** CLI-issued temp dir, retro audit save, always delegate ([#54](https://github.com/dblanken-yale/not-me/issues/54)) ([505ceaf](https://github.com/dblanken-yale/not-me/commit/505ceafc056006de5a19e513076d4be6efaa0752))


### Documentation

* authority, prohibitions, parking and write-back references ([#4](https://github.com/dblanken-yale/not-me/issues/4)) ([ea958e6](https://github.com/dblanken-yale/not-me/commit/ea958e63fee8d53c2fef5badf198065b128e34b1))
* process references for verification, gate, branching, autonomy and delegation ([#9](https://github.com/dblanken-yale/not-me/issues/9)) ([6307d12](https://github.com/dblanken-yale/not-me/commit/6307d121196314f14b428ce30173ee8ffa62582e))
* workflow and testing guides; READMEs list every skill ([#25](https://github.com/dblanken-yale/not-me/issues/25)) ([a854740](https://github.com/dblanken-yale/not-me/commit/a8547403887b27a8466e17d0a01bd3c0b002968c))


### Refactoring

* guard composes the shared write gate and run-log helpers ([#13](https://github.com/dblanken-yale/not-me/issues/13)) ([50840d8](https://github.com/dblanken-yale/not-me/commit/50840d8f508b7a5d275d7db24257f86ae16eb296))

## Unreleased
