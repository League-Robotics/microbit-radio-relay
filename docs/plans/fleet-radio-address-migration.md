# Fleet radio-address migration: move every robot to the 73-channel map

**Written:** 2026-09-14 · **Orchestrator:** the `microbit-radio-relay` agent (session
`microbit-radio-relay-d9`) keeps §7, and **every owner reports to it directly**
· **Status (newest, 2026-09-14 afternoon):** Eric brought every robot back to the
farm and asked for the finish. **torture is ready:** mbrelay 0.20260913.2 (the latest
release; the repo has nothing unreleased), all four pool relays run firmware
0.20260913.2, no sessions. **Registry cleared:** every Phase 0 pin removed. All 8 names
are `derived` on the new map (gopiv 12/30, tigez 52/179, tovez 48/29, vevov 20/82,
togov 64/45, zeguz 71/199, zetuv 49/250, vitut 41/30), with 0 conflicts and 0 channel
conflicts. The per-robot "clear the pin" step no longer exists; torture already answers
the new pair for every robot. **Farm now:** gopiv on **meili**, tigez on **loki**,
tovez on hodr, vevov on magni (done). Eric is directing the remaining reflashes.
cf read-only check: tovez still `calibration-0.20260913.1 1.20260912.8` (old 55/108);
tigez's and gopiv's farm serial links are `ERR busy` (probably Eric's robot-console), so
those two get verified through torture with `mbrelay connect` after their flash.
· **Status (before that):** vevov fully migrated except robot-console's usb rows. **Paused:** robot-console-71 has been moved to other work by Eric. It is still D's owner, but don't send it "flashing <robot>" until Eric resumes it. Its row rewrite is scripted as `phase4-relink.sh <robot> apply` in its scratchpad and needs Eric's approval. tovez, tigez and gopiv stay on hold until Eric resumes D or says to go torture-only.
· **Earlier:** Phases 0–2 done; **Phase 3 go given 2026-09-14.** D merged; calibration v0.20260914.5 released. Waiting on Eric to restart `npm run dev`. **Blocker:** on the farm, only vevov is advertised. tovez, tigez and gopiv disappeared; hodr, meili and loki still answer ping. robot-console's store shows those three links going stale **10:40–10:43**. Order is now vevov, then the others as they reappear

> **Owners (final, 2026-09-14):** A + F `microbit-radio-relay-d9` · B `radio-robot-lib-29`
> · C `pxt-nezha-diffdrive-ef` · D `robot-console-71` · E `nezha-robot-template-cf`.
> `robot-console-ef` and `nezha-robot-template-79` are **not** on this. Three sessions
> each claimed to coordinate; this line settles it.

> **Names:** the relay on Eric's Mac is **vevav** (its banner says so). **vevov** is a
> robot (NEZHA2, answers on 37/43). Don't mix them up.

This is a shared plan for every agent working on the fleet. Read it all before
changing anything. Report to the orchestrator (see *How we work together*) rather
than to each other, so one status table stays true.

---

## 1. The problem

`mbrelay connect tovez` from the relay host **torture** cannot reach tovez. A local
relay (vitut, driven by robot-console) reaches it fine on **channel 55, group 108**.
Torture tunes to **48 / 29**.

Neither side is broken. They use **two different maps** from a robot's name to its
radio address, and the fleet is half-way between them.

A micro:bit's five-letter name decodes to a number `n` (base 5, consonants `zvgpt` at
letters 0/2/4, vowels `uoiea` at 1/3, first letter most significant). That decoding
has not changed. What changed is the step from `n` to channel and group:

| map | channel | group | tovez (n = 2665) | status |
|---|---|---|---|---|
| **old** | `25 + 2*(n % 25)` (odd, 25–73) | `1 + n // 25`, then +1 if ≥ 10 (1–9, 11–126) | 55 / 108 | retired |
| **new** | `11 + (n % 73)` (11–83) | `15 + (n % 241)` (15–255) | **48 / 29** | **the spec** |

The old map has only 25 channels. Channel is the actual radio frequency; group is
only a filter applied after a packet arrives. So robots with the same channel share
the air even when their groups differ, and with 25 channels that happens early
(vevov and togov are both on 37 today). The new map uses all 73 legal channels.

### Who is on which map today

| part | map | how it gets its pair |
|---|---|---|
| radio-robot-lib spec `docs/design/radio-addressing.md` | **new** | the normative definition |
| microbit-radio-relay: `mbrelay` registry, `naming.py`, firmware `!N` / `naming.h` | **new** | computes the spec; deployed on torture (0.20260913.2) |
| radio-robot-lib `config/robots/*.json` | old | **pins** `connection.radio_channel` / `radio_group` per robot |
| pxt-nezha-diffdrive robot firmware | old | `tools/make_deploy.py` bakes the pair from that config into `kChannel`/`kGroup` |
| pxt-nezha-diffdrive tools | old | `make_deploy.derive_radio_from_name()`, `tools/robotlink.py`, `tools/field_calibration.json` overrides, `docs/radio-addressing.md` + vectors |
| robot-console | old | `packages/protocol/src/radioAddress.ts` (`nameToRadioAddress`, `radioAddressToName`, `validateRadioAddress`), vendored pxt spec, persisted DB overrides |
| nezha-robot-template | old | `league-projects/scratch/nezha-robot-template/test/boot.ts` derives at boot |
| mbrelay on **vali** | old | still 0.20260831.1 |

**Root cause:** the spec changed and the relay moved, but no robot moved. A robot
never works out its own address at boot; its build bakes in whatever pair its config
says. So changing the formula moves nothing until the configs change and every robot
is rebuilt and reflashed.

---

## 2. Source of truth

- **Spec:** `/Volumes/Proj/proj/RobotProjects/radio-robot-lib/docs/design/radio-addressing.md`
  (also the radio-robot-lib wiki page `radio-addressing`). If this plan and the spec
  disagree, **the spec wins**. Tell the orchestrator.
- **Reverse map** (pair → name): `n = (c-11) + 73 * (((g-15) - (c-11) + 241) * 208 % 241)`.
  Reject a pair unless `11 ≤ c ≤ 83`, `15 ≤ g ≤ 255` and `n < 3125`. Most pairs
  belong to no name.
- **Machine-readable vectors:** `microbit-radio-relay/server/tests/radio-address-vectors.json`,
  transcribed from the spec.
- **The gate: digest D2.** For n = 0..3124 in order, emit
  `<name>,<channel>,<group>,<decode(name)>,<reverse(channel,group)>\n`, then take the
  sha256 of the whole output:
  `305d6ee08cfae978fe13e1179c6047a56e1b0b1abe23c2cb757f01461cf2d35f`.
  D1 (forward only, `<name>,<channel>,<group>\n`) is
  `c22691f1c47bed3ac5317119487a30ea8fd0224d61c50bba551b1e624b548a84`.
  D1 is useful for narrowing down a failure; D2 is the one to pass.
  If your D1 is `3ce51760…`, your **encoder** is little-endian. If your D2 is
  `df3d1db0…`, only your **decoder** is.
- **Cross-repo check:** every implementation exposes `tools/radio-address-dump`
  (contract in `microbit-radio-relay/tools/radio-address-dump`: `--list`, then
  `<impl> [1|2]` prints the 3125 lines, or exits 3 when it cannot run here). Then run, from
  microbit-radio-relay:

  ```bash
  just conformance          # this repo plus every sibling with tools/radio-address-dump
  ```

  It fails if any implementation disagrees with another or with D2, and names the
  first name that differs. `auto` only scans siblings in `RobotProjects/`, so the
  orchestrator runs it with explicit targets:

  ```bash
  just conformance auto /Volumes/Proj/proj/league-projects/microbit/robot-console \
      /Volumes/Proj/proj/league-projects/scratch/nezha-robot-template
  ```

---

## 3. The fleet

Robots only. **Relays are not re-addressed.** getez, gozop, guvov and zetog (torture),
zavaz (vali), and vitut and vevav on Eric's Mac are tuned per session with `!CG` / `!N`.

| robot | firmware | pinned now (config) | extra pins elsewhere | **new pair** |
|---|---|---|---|---|
| gopiv | NEZHA2 (pxt) | 47 / 60 | — | **12 / 30** |
| tigez | pxt? | 55 / 114 | pxt `tools/field_calibration.json` 55/114 | **52 / 179** |
| togov | pxt? | 37 / 109 | — | **64 / 45** |
| tovez | NEZHA2 (pxt) | 55 / 108 | — | **48 / 29** |
| vevav | ⚠ confirm | 67 / 43 | — | **35 / 97** |
| vevov | NEZHA2 (pxt) | 37 / 43 | pxt `tools/field_calibration.json` 37/43 | **20 / 82** |
| zeguz | NEZHA2 (pxt) | 25 / 19 | — | **71 / 199** |
| zetuv | NEZHA2 (pxt) | 27 / 21 | — | **49 / 250** |

No two robots share a channel under the new map.

**Scope, Eric 2026-09-14: only gopiv, tigez, tovez and vevov are active.** togov, zeguz
and zetuv are boxed and inactive, so they are **not flashed**. Their torture pins stay,
and B.1 still gives their configs the new pair, with a note that they are inactive and
must get the calibration release (and have their pin cleared) when they come back.

**Eric's decisions (2026-09-14, reported by radio-robot-lib-29 and robot-console-71):**
the Mac relays are vitut and vevav; the Phase 3 firmware is the nezha-robot-template
calibration release; B and D run outside the CLASI process; agents may flash farm
robots and use vitut/vevav without asking first. Flashing still follows the per-robot
handshake with the orchestrator in §5, so torture pins are cleared in step.

**vevav, decided by Eric 2026-09-14:** vevav **stays a relay** (on the Mac, with vitut)
and is **out of the migration**. B.1 leaves the pair in `config/robots/vevav.json`
unchanged and adds a note there saying the board is a relay, so no one builds a robot
image for it.

⚠ **Robots missing from `devices.json`:** tigez and togov are absent from
radio-robot-lib's `config/robots/devices.json`, so confirm their firmware family when
they show up on the farm.

---

## 4. Workstreams

Each workstream lists its owner, what to change, and when it counts as done. Finish
the code (Phase 1) without touching any robot or any shared config.

### A. microbit-radio-relay: owner **orchestrator**

Already done: `naming.py`, `source/relay/naming.h`, firmware `!N` / `!N?`, the registry
(`[registry.names]` pin > `names.json` > derived), channel-conflict reporting,
`tools/radio-address-dump` (`python`, `firmware-cpp`), conformance runner, and the
0.20260913.2 deploy on torture.

To do:
1. **Phase 0, transition pins.** On torture, pin every robot to the pair it is really
   on today, so `mbrelay connect <robot>` works during the migration:
   `mbrelay names set <robot> <old pair>` for each row in §3. Each pin is cleared the
   moment that robot is reflashed and verified (Phase 4).
2. Run `just conformance` after each peer lands its dump tool; report results here.
3. **Phase 4, verify each reflashed robot:** clear its pin, then
   `mbrelay connect <robot>` (tunes by the registry, then PINGs the robot). The robot
   passes when it answers.
4. Upgrade **vali** to ≥ 0.20260913.2. It still derives the old map.
5. Record the finished state in the relay wiki and in this plan.

### B. radio-robot-lib: owner **radio-robot-lib-29**

1. **Phase 2:** in each `config/robots/<robot>.json`, set
   `connection.radio_channel` / `radio_group` to the **new pair** from §3, in one
   commit. Keep the `_radio_note`, but name the spec.
2. Update `robot_config.schema.json`. Its `radio_channel` / `radio_group` descriptions
   still cite pxt's old map ("odd values 25..73", "1..9 and 11..126"). The new text
   should say the value comes from `docs/design/radio-addressing.md`, and that
   11–83 / 15–255 is the name-derived range.
3. **Phase 5:** change the spec's *Status* paragraph from "not yet adopted by
   pxt-nezha-diffdrive … nor by this repo's fleet configs" to adopted, and update the
   wiki page to match.

Order matters. **B.1 lands only after C.1–C.3 have merged,** because pxt's
`make_deploy.py` today treats an odd 25–73 channel as name-derived and would
misclassify the new pairs.

### C. pxt-nezha-diffdrive: owner **pxt-nezha-diffdrive-ef**

Canonical checkout: `/Volumes/Proj/proj/RobotProjects/pxt-nezha-diffdrive` (clean, up to
date). Do **not** use `league-projects/microbit/pxt-nezha-diffdrive` (16 commits behind,
dirty).

1. `tools/make_deploy.py`:
   - `derive_radio_from_name()`: the new map. Update its docstring.
   - `_read_robot_radio_group()`: the "half-migrated config" check keys on
     `channel % 2 == 1 and 25 <= channel <= 73`, i.e. the old channel space. Replace
     it with a check against the new map. A config whose channel is the name-derived
     channel but that names no group must still be refused; judge that with the new
     formula, or require both fields and drop the heuristic.
   - The `radio: … [name-derived would be …]` line then reports the new pair.
2. `tools/robotlink.py` `radio_address()`: its fallback derivation follows (1).
   `tools/field_calibration.json` has explicit `radio_channel` / `radio_group` for
   **vevov (37/43)** and **tigez (55/114)**, and those override everything. Remove them
   in Phase 3, when those robots are reflashed, so the new derived pair is used.
3. `docs/radio-addressing.md` + `docs/radio-address-vectors.json`: replace them with
   the new map, or with a pointer to radio-robot-lib's spec plus the transcribed
   vectors. Update tests (e.g. `tests/tools/…`) that hard-code old pairs.
4. Add `tools/radio-address-dump` (see §2) exposing every implementation here, e.g.
   `python` (`make_deploy.derive_radio_from_name`), plus `makecode-ts` if the
   extension computes it anywhere. **Pass D2.**
5. Grep for anything else that assumes the old space: docs under
   `docs/robot-connections.md`, `docs/robot-garage-start.md`, Robot Garage wiki pages,
   `src/comms/radio_transport.h` comments, and bench scripts with channel literals.
6. **Bump `pyproject.toml`'s version before building.** `ID` reports only that version,
   so two different builds would look identical. See the pxt memory note on bumping
   the version before a flash.
7. **Phase 3:** E now does the flashing (it owns the hex and already mapped the
   farm). C's Phase 3 job is only removing the vevov and tigez radio overrides from
   `tools/field_calibration.json` as those two robots move.
8. **No firmware range change is needed** (nezha-robot-template-cf):
   `setupRadio(int,int)` casts straight to `uint8_t`, so 83 / 255 reach the radio.
   Check only that no TS or block-level range on `setupRadio` assumes the old space.

### D. robot-console: owner **robot-console-71**

1. `packages/protocol/src/radioAddress.ts`:
   - `nameToRadioAddress`: the new map.
   - `radioAddressToName`: the new reverse, with the §2 rejection rules. Drop the
     odd-channel and group-10 rules; the new map never emits group < 15.
   - `validateRadioAddress`: the new derived space (11–83 / 15–255, `n < 3125`).
   - Update the module comment. Its normative-spec pointer must name radio-robot-lib's
     spec, not the vendored pxt doc.
2. **Check every caller of `validateRadioAddress`.** `packages/protocol/src/relay/commands.ts`
   uses it to gate `!CG` / `!CGT`. A relay tune must accept any hardware-valid pair
   (0–83 / 0–255), not just derived ones, or registry-pinned and moved robots will be
   refused. Decide, and test.
3. Tests: `radioAddress.test.ts` full-space conformance against **D2**, and the
   endianness vectors. Also fix `projection.test.ts`, `mbrelayRegistry.test.ts`,
   `relayBridger.test.ts` and anything asserting old pairs.
4. Add `tools/radio-address-dump` exposing the TS implementation (e.g. `node`/`ts`).
   **Pass D2.**
5. The vendored `vendor/pxt-nezha-diffdrive` copy still carries the old spec and
   vectors. Re-vendor after C.3 lands, or stop treating it as normative.
6. **The host DB has two places that store an address** (robot-console-71, verified in
   source 2026-09-14):
   - `devices.radio_channel` / `radio_group` / `radio_source`, rows with `override` or
     `registry`;
   - **every radio child link row** (`links`, `transport=radio`) stores its own
     channel/group, and on a named connect that beats everything
     (`server.ts` ~949). Examples today: `radio-vevov-via-usb-…` {37,43},
     `radio-gopiv-via-mbrelay-torture` {47,60}, `radio-tovez-via-mbrelay-torture`
     {55,108}, and a stale `radio-vevov-via-mbrelay-torture` {20,82}.

   In Phase 4, per robot as it moves, clear or rewrite both kinds to the new pair and
   report which rows changed. Until then those rows keep the old pairs working, even
   after D is merged.
7. Docs: `docs/design/specification.md` §6, `architecture.md`, and the
   `RadioAddressDialog` text.
8. robot-console holds the Mac's USB relay ports and farm links. Other agents must
   not kill it; drive it through its own interface.
9. **Gate answer (robot-console-71, 2026-09-14): the Mac USB relay paths do NOT ask
   torture's registry.** A USB relay (vitut/vevav) goes override → local derive, the
   default failover in `relayBridger.ts` ~446 derives only, and "local-derived" is used
   whenever the registry is unreachable. So **D lives on its own branch (a worktree, not
   `sprint/018`) and merges at Phase 3.**

   The original warning, kept for the record:
   ⚠ **Merge gate (robot-console-ef):** Eric runs `npm run dev` live from robot-console's
   working branch. The new map must not reach that branch before Phase 3 **unless**
   every path that tunes a relay (Mac USB relays vitut/vevav included) asks torture's
   registry first. The Phase 0 pins return the old pairs, so a registry-first path
   moves nothing. A local derivation, a stored row, or the "local-derived" fallback
   when the registry is unreachable would move robot-console to new pairs while the
   robots are still on old ones. Otherwise land D behind a branch or flag and merge
   in Phase 3.

### E. nezha-robot-template: owner **nezha-robot-template-cf**

The live checkout is `league-projects/scratch/nezha-robot-template`.
`league-projects/microbit/nezha-robot-template` is a stale clone of the same repo;
ignore it.

1. `test/boot.ts` is what sets the radio for the calibration release, with `setupRadio`
   at boot. **Correction (nezha-robot-template-79):** the committed boot.ts (HEAD and
   origin/master) does not derive anything. It hardcodes `setupRadio(55, 114)`, tigez's
   old pair, **for every robot**, so a robot running a current calibration release is
   on 55/114 whatever its name. nezha-robot-template-cf's uncommitted change derives
   the new map from `control.deviceName()`; commit that. **This is the address every
   robot will have after Phase 3.**
2. Give it a `tools/radio-address-dump` (a node port of boot.ts's own derivation) and
   pass **D2**.
3. Bump the nezha-diffdrive pin (currently v1.20260913.1) once C ships a release
   containing the new map, then cut the calibration release that Phase 3 flashes.

### F. radio-robot-elite: owner **orchestrator** (check only)

`src/scripts/gen_boot_config.py` bakes `connection.radio_channel` (channel only, no
group) from the same radio-robot-lib configs. Confirm whether any robot still runs
elite firmware. If one does, it gets rebuilt in Phase 3 like the others; if none does,
record that here and leave it alone.

**Finding (2026-09-14):** the banner cannot tell the two firmwares apart. Elite's
`platform/microbit/microbit_banner.cpp` prints `DEVICE:NEZHA2:robot:<name>:<serial>`,
exactly like pxt's `protocol.cpp`. Settle it per robot in Phase 3 with `ID`: the pxt
calibration release answers `id diffdrive calibration-<tag> …`. Since Phase 3
reflashes every robot with that release anyway, an elite robot would simply stop
being one, so F needs no code change.

---

## 5. Phases and gates

| phase | what | who | gate to move on |
|---|---|---|---|
| **0** | Pin every robot's **old** pair in torture's registry | A | `mbrelay connect tovez` from a Mac works again |
| **1** | Code in C, D, E (and B.2 schema text). **No robot, no shared config, no DB touched.** | C, D, E, B | Each repo's tests are green, its `tools/radio-address-dump` passes D2, and `just conformance` is green across relay + pxt + robot-console |
| **2** | radio-robot-lib configs → new pairs (B.1) | B | C.1 has merged; `make_deploy.py --robot <r>` (build only) prints the new pair with no mismatch note |
| **3** | **Go conditions:** the 4 active robots on the farm ✔, D Phase 1 green. **Sequence:** orchestrator's go → D merges into sprint/018, E merges `radio-map-73` and CI publishes the calibration release → **Eric restarts `npm run dev`** (asked by D; only Eric restarts it) → then **E flashes** tovez, tigez, gopiv, vevov one at a time. Before each robot E tells the orchestrator; its torture pin is cleared and D rewrites its link rows in the same step | E (release + flashing), D (merge + rows), Eric (restart) | Every active robot answers `HELLO`/`ID` with the new release's version |
| **4** | Per robot: clear the relay pin, `mbrelay connect <robot>`, clear stale robot-console DB overrides | A, D | Every robot answers on its new pair through torture **and** through robot-console |
| **5** | Docs, wikis, spec status, vali upgrade, template pin | all | This plan's status table is complete |

### Phase 3 procedure (farm flash)

Farm nodes are **hodr, meili, magni and loki**, running `mbdeploy serve`. Boards move
between nodes, so re-list every time. A farm link answering `ERR busy` is held by
another client (often robot-console). Don't push past it.

Eric's goal: every robot on the new **nezha-robot-template calibration release**, and
reachable both through the Mac's USB relays (vitut, vevav) and through torture.

```bash
mbdeploy list --remote                                    # find each robot and its node
mbdeploy deploy <robot> --remote --hex <calibration hex>  # flash over the network
```

The calibration hex is **one image for every robot**, published by
nezha-robot-template's own `release.yml` (tags `v0.YYYYMMDD.n`). It never bakes
`kChannel`/`kGroup`; the pair comes from boot.ts at runtime, so radio-robot-lib's
config pins do not affect it. `ID` answers
`id diffdrive calibration-<tag> <ext version> <name>`, and `STATUS` reports the live
radio channel and group, which is the check for Phase 4.

Builds made with `make_deploy.py --robot <robot>` still read radio-robot-lib's config
pair (Phase 2), so they must also land on the new pair. Check their
`radio: channel C group G` line.

⚠ **Before flashing, know each robot's current pair.** It depends on the firmware (`ID`):
- **Calibration release ≤ v0.20260912.5:** boot.ts hardcoded `setupRadio(55, 114)`, so
  the robot is on **55/114** whatever its name.
- **Calibration release v0.20260913.1 … v0.20260914.x (from 6de1df9):** boot.ts
  derives the robot's **old-map** pair from its name, the §3 "pinned now" column.
- **A make_deploy build:** its radio-robot-lib config pair, which is also the §3 column.
- `STATUS` reports channel/group only from nezha-diffdrive 1.20260914.1 on. On older
  builds, infer the pair from `ID` as above, or confirm it through a relay.

Rules learned the hard way:
- **Claim a robot in the status table first** (message the orchestrator). Only one
  flasher per robot.
- A timeout during `Programming...` leaves the board in an **unknown state** (it may
  be half-written). Do not treat it as rolled back; flash again.
  CTRL-AP mass-erase recovery is routine on this fleet.
- **A mass erase wipes WiFi credentials** (flash page 0x7D000). Re-provision WiFi
  (`WIFICRED SET`) after flashing any robot that uses WiFi (tovez, gopiv, …).
- Verify from the chip: `HELLO`, then `ID` shows the bumped version. Then Phase 4 proves
  the radio address through a relay.
- Relays on torture are shared. Check `mbrelay devices --remote` for sessions before
  taking one.

---

## 6. How we work together

- **Report to the orchestrator directly** with `SendMessage` to
  `microbit-radio-relay-d9`: what finished, the commit sha, the test and D2 result,
  and anything blocked. There is no intermediate coordinator. The orchestrator
  updates §7 and tells whoever is waiting on you.
- **Ask before crossing into another workstream's files.** If your change needs
  something in another repo, message the orchestrator; don't edit it yourself.
- **Shared state has one writer each:** torture's registry (A), radio-robot-lib configs
  (B), robot flashing (C, one robot at a time), robot-console's DB (D).
- **Found a disagreement with the spec?** Stop and report it. Do not fork the formula.

---

## 7. Status (maintained by the orchestrator)

### Workstreams

| stream | owner | phase 1 | phase 2+ | notes |
|---|---|---|---|---|
| A relay | microbit-radio-relay-d9 | done | Phase 0 pins set 2026-09-14; vevov and tovez answered through them | vali unreachable 2026-09-14 (no ping or ssh); upgrade blocked |
| B radio-robot-lib | radio-robot-lib-29 | **B.2 done:** `f4f7c68` + version bump `ad6039c` (0.20260914.1) on main; 736 tests passed | **Phase 2 done 2026-09-14:** B.1 `1aaf1d6` + bump `8dcea50` (0.20260914.2), 736 passed. **Gate verified by the orchestrator:** make_deploy's own `_read_robot_radio_channel`/`_group` return the new pair for all 7 robots, equal to `derive_radio_from_name` (no mismatch). Left as is on purpose: frozen fixture `tests/host/rogo/fixtures/gopiv.json` (47/60), historical `_provenance` text. B.3 waits for Phase 5 | Eric: B commits directly to main, outside the CLASI process; vevav.json pair left as is, with a relay note |
| C pxt-nezha-diffdrive | pxt-nezha-diffdrive-ef | **landed** `298b0de` (pushed): make_deploy + reverse map on the new map, half-migrated check keys on the robot's own derived channel, dump `python` matches D2, docs → pointer + transcribed vectors. Formal report (tests) pending | Phase 3: remove vevov/tigez overrides in `field_calibration.json` as they move | transitional: robotlink's name derivation already gives new pairs for robots without overrides (gopiv, tovez), so robotlink misses them until they are flashed |
| D robot-console | robot-console-71 | **done** 2026-09-14: `b8ce4f4`, `3b8ce54`, `8060f18` on `radio-map-73` (worktree `robot-console-radio-map-73`); `npm test` 2292 pass, typecheck clean; dump `ts` matches D2; `!CG`/`!CGT` accept any hardware pair 0–83 / 0–255; vendored pxt spec no longer normative; no legacy-pair retry (not needed). **Merged** into sprint/018 (fast-forward b90ef47 → 8060f18). Store `.backup` taken (integrity ok; 16 radio links, 7 devices; rows untouched). Restart trap found and fixed: the host loads `@robot-console/protocol` from `packages/protocol/dist`, which still held the old map, so dist was rebuilt in Eric's checkout (now tovez 48/29, tigez 52/179, gopiv 12/30, vevov 20/82). Waiting for Eric to restart `npm run dev` | Per robot on "flashing <robot>": `UPDATE links SET address=json_set(address,'$.channel',C,'$.group',G) WHERE transport='radio' AND id LIKE 'radio-<name>-via-%'` (5 s busy timeout; one `.backup` before the first robot). 16 rows: gopiv 5 @47/60, tigez 4 @55/114, tovez 3 @55/108, vevov 3 @37/43 (+ torture row already 20/82). `devices.*`: nothing set for the 4 robots | gate answered: USB relay paths skip the registry (bridger failover, relaySweeper.ts:673, local-derived), so no early merge. **Eric must restart `npm run dev` after D merges, before the first flash**; only Eric restarts it |
| E nezha-robot-template | nezha-robot-template-cf | **done** 2026-09-14: `465ce00` on branch `radio-map-73`; boot.ts + leaguebot on the new map; `makecode-ts` and `leaguebot-js` dumps pass D2; tests 37/37 | **Released:** calibration **v0.20260914.5** @ 465ce00 (master fast-forwarded), `MICROBIT.hex` sha256 `be2e04346e89aaefd8e7371764080b1a2b08f9434199bf9ddf6e2b264799031f`, profile `calibration-0.20260914.5`, extension v1.20260914.1. Holding for "go vevov" | live checkout: scratch/; microbit/ is a stale clone; -79 stood down, changed nothing |
| F radio-robot-elite | microbit-radio-relay-d9 | check pending | — | |

### Conformance runs

| when | command | result |
|---|---|---|
| 2026-09-14 | `radio_address_conformance.py auto …/scratch/nezha-robot-template` | **all agree, all match D2:** relay `python` and `firmware-cpp`, pxt-nezha-diffdrive `python`, nezha-robot-template `makecode-ts` and `leaguebot-js`. robot-console dump not landed yet. |
| 2026-09-14 | `radio_address_conformance.py auto …/robot-console-radio-map-73 …/scratch/nezha-robot-template` | **Phase 1 gate passed. All six implementations agree and match D2:** relay `python` + `firmware-cpp`, pxt `python`, robot-console `ts` (8060f18), template `makecode-ts` + `leaguebot-js`. |

### Robots

| robot | farm node (2026-09-14) | firmware now (`ID`) | relay pin (old pair) | config → new | flashed (version) | verified via torture | robot-console DB cleared |
|---|---|---|---|---|---|---|---|
| gopiv | loki (link `ERR busy`); **no longer advertised** on the farm (later 2026-09-14 recheck) | `id diffdrive …` (radio reply truncated) | 47/60 ✔ (answered PING via getez) | | | | |
| tigez | meili; **no longer advertised** on the farm (later 2026-09-14 recheck) | calibration-0.20260914.2, ext 1.20260913.1 | 55/114 ✔ (answered PING via guvov) | | | | |
| togov | **boxed, inactive (Eric): not flashed** | — | 37/109 ✔ (kept) | | | | |
| tovez | hodr; **no longer advertised** on the farm (later 2026-09-14 recheck) | calibration-0.20260913.1, ext 1.20260912.8 | 55/108 ✔ (answered) | | | | |
| vevav | — (**stays a relay**, Eric 2026-09-14; out of the migration) | | | | | | |
| vevov | magni. Serial link was held by Eric's robot-console (`mbserial-vevov`); **released** by robot-console-71 (`session-close`, user_closed=1) and confirmed free by the orchestrator. **First to flash** | `id diffdrive calibration-0.20260913.1 1.20260912.8 vevov` (old-map per-name pair 37/43; stored WiFi creds, ssid Busboom_Garage) | **cleared** 2026-09-14 after Eric's restart (pid 78300); registry now answers 20/82 (derived) | ✔ 20/82 (B.1) | ✔ **calibration-0.20260914.5 / 1.20260914.1**, 13:01:14–13:01:47, sector erase (WiFi creds kept); over magni STATUS `channel=20 group=82` | ✔ getez tuned to 20/82 → PING + `ID` answered | | **Blocked:** robot-console-71's permission layer declined the write to Eric's live DB ("modify shared resources"). Eric must approve it or edit the rows himself. The 3 usb rows (…07d057b7…, …2e78ea8f… vevav, …8939f0a5… vitut) are still 37/43; the torture row is already 20/82. **Via torture row: verified by robot-console-71** (connected in 1.4 s, ID calibration-0.20260914.5, STATUS channel=20 group=82 steady; session left open). WiFi: **no working ESP module** (cf, 80 s over magni: empty `reply=` for every AT command, `wifi=0` in all STATUS lines; both credential slots intact). Not a regression; **vevov done**, apart from the usb rows |
| zeguz | **reactivated 2026-09-14:** plugged into Linux lab machine **erdos** for robot-console .deb testing; robot-console-71 flashing it with the latest calibration release from erdos (outside the farm handshake) | — | cleared (registry derives 71/199) | ✔ 71/199 (B.1) | in progress (robot-console-71) | n/a (on erdos USB, not torture's range) | erdos's own store |
| zetuv | **boxed, inactive (Eric): not flashed** | — | 27/21 ✔ (kept) | | | | |
