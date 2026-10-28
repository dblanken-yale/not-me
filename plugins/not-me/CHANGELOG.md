# Changelog

## [0.6.0](https://github.com/dblanken-yale/not-me/compare/not-me-v0.5.0...not-me-v0.6.0) (2026-10-28)


### Features

* /not-me:suggest recommends unassigned issues for the operator to take ([#106](https://github.com/dblanken-yale/not-me/issues/106)) ([409df0e](https://github.com/dblanken-yale/not-me/commit/409df0e8440a350511b4fd53f4ea010231ee8006))
* **102:** keep labels and board status in step with queue.sync ([#107](https://github.com/dblanken-yale/not-me/issues/107)) ([d4d7438](https://github.com/dblanken-yale/not-me/commit/d4d7438d541712d9433874d956542ff025a7daff))
* **110:** pick from a to-do label or a board To Do column ([#114](https://github.com/dblanken-yale/not-me/issues/114)) ([cc7ba4f](https://github.com/dblanken-yale/not-me/commit/cc7ba4f04278623bf152c551fc769eb79bb25758))
* **113:** let an issue declare it may stack on an open PR ([#116](https://github.com/dblanken-yale/not-me/issues/116)) ([28933e3](https://github.com/dblanken-yale/not-me/commit/28933e317b5302795e93db039005cbe7c8b3b807))
* **72:** add a picking step to setup that finds ranking fields and labels ([#104](https://github.com/dblanken-yale/not-me/issues/104)) ([46a9235](https://github.com/dblanken-yale/not-me/commit/46a9235d54770a441ed2b76302273119702e7778))
* let the operator choose and switch the queue source ([#101](https://github.com/dblanken-yale/not-me/issues/101)) ([fb74926](https://github.com/dblanken-yale/not-me/commit/fb7492629495fa8f897f99e23817ab3503f867dc))
* report and fix label and board Status drift ([#115](https://github.com/dblanken-yale/not-me/issues/115)) ([364ca2d](https://github.com/dblanken-yale/not-me/commit/364ca2dddac6d4f01904613ada54495ba2f0d331))
* start pr bodies with a linked issue heading ([#100](https://github.com/dblanken-yale/not-me/issues/100)) ([c82d427](https://github.com/dblanken-yale/not-me/commit/c82d42765878b7a7d30ed7b837a1ee78f05729df))


### Bug Fixes

* **76:** keep the blocked input in the guard log, redacted ([#109](https://github.com/dblanken-yale/not-me/issues/109)) ([a4c5a22](https://github.com/dblanken-yale/not-me/commit/a4c5a2265e1ea59a4cfa875ed3bf5dfd758ab9d4))
* **77:** let grep, rg and sed patterns mention protected names ([#112](https://github.com/dblanken-yale/not-me/issues/112)) ([115b4f8](https://github.com/dblanken-yale/not-me/commit/115b4f8b8ac932962c00a9d91fb63c4d36f4b405))
* guard says 'no github origin' when a git -C path is a variable ([#108](https://github.com/dblanken-yale/not-me/issues/108)) ([030a740](https://github.com/dblanken-yale/not-me/commit/030a740c4ff4c0add549db1c40d69cb94453bc3b))
* report runs that died before log finish in notme status ([#99](https://github.com/dblanken-yale/not-me/issues/99)) ([d01fe74](https://github.com/dblanken-yale/not-me/commit/d01fe74911bd953df2bca0161b3d7a6a2f84f836))

## [0.5.0](https://github.com/dblanken-yale/not-me/compare/not-me-v0.4.0...not-me-v0.5.0) (2026-10-26)


### Features

* **50:** offer to fix failed checks in /not-me:doctor ([#89](https://github.com/dblanken-yale/not-me/issues/89)) ([650e66d](https://github.com/dblanken-yale/not-me/commit/650e66d1a537b9dc82e0b16526bdb09cffea1e04))
* pick-issue gathers facts for every eligible issue and new picks by impact ([#82](https://github.com/dblanken-yale/not-me/issues/82)) ([b93079b](https://github.com/dblanken-yale/not-me/commit/b93079b63300b067b45ecb7e4f80200f08aa17f4))
* set ready to merge only when every required reviewer approved ([#95](https://github.com/dblanken-yale/not-me/issues/95)) ([5f301f2](https://github.com/dblanken-yale/not-me/commit/5f301f22c6431a03011376c12447a9dd42964e6e))
* user-defined picking rules and a target epic in a Picking work config section ([#83](https://github.com/dblanken-yale/not-me/issues/83)) ([a8350e7](https://github.com/dblanken-yale/not-me/commit/a8350e7310fcacdcdeb873e43649624a380dd0a2))


### Bug Fixes

* **43:** keep the visual check path out of the PR ([#84](https://github.com/dblanken-yale/not-me/issues/84)) ([a828187](https://github.com/dblanken-yale/not-me/commit/a8281871a526b6e07c1d2da98276112532291850))
* **44:** let dry-run state moves assume the state earlier dry-run writes set ([#85](https://github.com/dblanken-yale/not-me/issues/85)) ([1b3fd35](https://github.com/dblanken-yale/not-me/commit/1b3fd35afb363239f3eb3ae7e7b76e28750efbbc))
* **45:** fall back to a general-purpose code review when the reviewer agent is missing ([#86](https://github.com/dblanken-yale/not-me/issues/86)) ([3b235a4](https://github.com/dblanken-yale/not-me/commit/3b235a443580c7babfe20ebaaf815fb66fc12918))
* **46:** define models.verifier auto as a different model than the run's ([#96](https://github.com/dblanken-yale/not-me/issues/96)) ([db17184](https://github.com/dblanken-yale/not-me/commit/db17184b08513140f9c1ec31f1bc62bb923f1515))
* **92:** retry a guard case once when the hook is killed by a signal ([#97](https://github.com/dblanken-yale/not-me/issues/97)) ([2d825f1](https://github.com/dblanken-yale/not-me/commit/2d825f1e757cd05030a35e9024b11c854efd0c77))
* dispatch every subagent in the foreground so bg runs finish ([#88](https://github.com/dblanken-yale/not-me/issues/88)) ([9c57c6d](https://github.com/dblanken-yale/not-me/commit/9c57c6d677f22152c250352dca997dea8abe8e66))

## [0.4.0](https://github.com/dblanken-yale/not-me/compare/not-me-v0.3.0...not-me-v0.4.0) (2026-10-23)


### Features

* **cli:** issue-body replaces a trusted author's issue body ([#68](https://github.com/dblanken-yale/not-me/issues/68)) ([c453ea8](https://github.com/dblanken-yale/not-me/commit/c453ea8d557b81268a1a7e0cb9dcb2dfcb9b57b4))
* pick only assigned issues; slim the guard and allow merges ([#81](https://github.com/dblanken-yale/not-me/issues/81)) ([a6a9eee](https://github.com/dblanken-yale/not-me/commit/a6a9eee890936c8bb2f3abaa2160e5b11c0790c2))

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
