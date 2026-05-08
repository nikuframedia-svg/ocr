# Cross-check report — ground_truth_draft/

- distinct OFs: **95**
- OFs that appear more than once: **18**
- OFs with divergent invariant fields: **13**

## Suspect OFs

An OF with divergent `cliente` / `modelo` / `comp_mm` / `larg_mm` across rows almost certainly has at least one wrong transcription. Open the listed sheets and compare.

### OF `257179`

Appears in:
- `JulioLima_2026.04.14.json` row 3: cliente=`MTG`, modelo=`CA08E08B-1ªP.`, comp_mm=`8720`, larg_mm=`875`
- `VitorCarvalho_2026.04.13.json` row 0: cliente=`MTG`, modelo=`CA08E08B-1ªPRIORIDADE`, comp_mm=`8720`, larg_mm=`875`

Divergent fields:
- **modelo**: `CA08E08B-1ªP.`, `CA08E08B-1ªPRIORIDADE`

### OF `257370`

Appears in:
- `VitorCarvalho_2026.04.10.json` row 2: cliente=`STOCK MTG GMBH`, modelo=`CGC2E06Di-2ªPRIORIDADE`, comp_mm=`7000`, larg_mm=`1400`
- `VitorCarvalho_2026.04.14.json` row 2: cliente=`STOCK MTG GMBH`, modelo=`CGC2E06Di-2ªPRIORIDADE`, comp_mm=`7000`, larg_mm=`712`

Divergent fields:
- **larg_mm**: `1400`, `712`

### OF `257372`

Appears in:
- `VitorCarvalho_2026.04.14.json` row 3: cliente=`STOCK MTG GMBH`, modelo=`CGC2F08Di`, comp_mm=`9200`, larg_mm=`800`
- `VitorCarvalho_2026.04.17.json` row 3: cliente=`STOCK MTG GMBH`, modelo=`CGC2E08Di`, comp_mm=`9200`, larg_mm=`800`

Divergent fields:
- **modelo**: `CGC2E08Di`, `CGC2F08Di`

### OF `260617`

Appears in:
- `JulioLima_2026.04.10.json` row 7: cliente=`MTG`, modelo=`LMF1861T`, comp_mm=`8100`, larg_mm=`1050`
- `JulioLima_2026.04.15-1.json` row 6: cliente=`MTG`, modelo=`LMF1882T`, comp_mm=`11900`, larg_mm=`1250`

Divergent fields:
- **modelo**: `LMF1861T`, `LMF1882T`
- **comp_mm**: `11900`, `8100`
- **larg_mm**: `1050`, `1250`

### OF `261221`

Appears in:
- `JulioLima_2026.04.15-1.json` row 0: cliente=`ENEDIS`, modelo=`CD03P502`, comp_mm=`10000`, larg_mm=`1200`
- `JulioLima_2026.04.15-1.json` row 7: cliente=`ENEDIS`, modelo=`CD03P503`, comp_mm=`11000`, larg_mm=`1250`
- `JulioLima_2026.04.15.json` row 2: cliente=`ENEDIS`, modelo=`CD03P504`, comp_mm=`12000`, larg_mm=`1250`
- `JulioLima_2026.04.15.json` row 3: cliente=`ENEDIS`, modelo=`CD03P504`, comp_mm=`12000`, larg_mm=`1250`
- `VitorCarvalho_2026.04.15.json` row 0: cliente=`ENEDIS`, modelo=`CD02P11B-CD02P503`, comp_mm=`11000`, larg_mm=`1100`

Divergent fields:
- **modelo**: `CD02P11B-CD02P503`, `CD03P502`, `CD03P503`, `CD03P504`
- **comp_mm**: `10000`, `11000`, `12000`
- **larg_mm**: `1100`, `1200`, `1250`

### OF `261567`

Appears in:
- `JulioLima_2026.04.15-1.json` row 1: cliente=`ENEDIS`, modelo=`CD03P502`, comp_mm=`10000`, larg_mm=`1200`
- `JulioLima_2026.04.15-1.json` row 10: cliente=`ENEDIS`, modelo=`CD03P503`, comp_mm=`11000`, larg_mm=`1250`
- `JulioLima_2026.04.15.json` row 1: cliente=`ENEDIS`, modelo=`CD03P504`, comp_mm=`12000`, larg_mm=`1250`

Divergent fields:
- **modelo**: `CD03P502`, `CD03P503`, `CD03P504`
- **comp_mm**: `10000`, `11000`, `12000`
- **larg_mm**: `1200`, `1250`

### OF `261571`

Appears in:
- `JulioLima_2026.04.15-1.json` row 2: cliente=`ENEDIS`, modelo=`CD03P502`, comp_mm=`10000`, larg_mm=`1200`
- `JulioLima_2026.04.15-1.json` row 11: cliente=`ENEDIS`, modelo=`CD03P503`, comp_mm=`11000`, larg_mm=`1250`
- `JulioLima_2026.04.15.json` row 6: cliente=`ENEDIS`, modelo=`CD03P504`, comp_mm=`12000`, larg_mm=`1250`

Divergent fields:
- **modelo**: `CD03P502`, `CD03P503`, `CD03P504`
- **comp_mm**: `10000`, `11000`, `12000`
- **larg_mm**: `1200`, `1250`

### OF `261605`

Appears in:
- `JulioLima_2026.04.18.json` row 7: cliente=`COMATELEC`, modelo=`1383VF01`, comp_mm=`8102`, larg_mm=`1500`
- `JulioLima_2026.04.18.json` row 8: cliente=`COMATELEC`, modelo=`1383VF00`, comp_mm=`8102`, larg_mm=`1500`

Divergent fields:
- **modelo**: `1383VF00`, `1383VF01`

### OF `261861`

Appears in:
- `JulioLima_2026.04.15-1.json` row 5: cliente=`ENEDIS`, modelo=`CD03P502`, comp_mm=`10000`, larg_mm=`1200`
- `JulioLima_2026.04.15.json` row 0: cliente=`ENEDIS`, modelo=`CD03P503`, comp_mm=`11000`, larg_mm=`1250`
- `JulioLima_2026.04.15.json` row 4: cliente=`ENEDIS`, modelo=`CD03P504`, comp_mm=`12000`, larg_mm=`1250`
- `JulioLima_2026.04.15.json` row 5: cliente=`ENEDIS`, modelo=`CD03P504`, comp_mm=`12000`, larg_mm=`1250`

Divergent fields:
- **modelo**: `CD03P502`, `CD03P503`, `CD03P504`
- **comp_mm**: `10000`, `11000`, `12000`
- **larg_mm**: `1200`, `1250`

### OF `261870`

Appears in:
- `JulioLima_2026.04.15-1.json` row 4: cliente=`ENEDIS`, modelo=`CD03P502`, comp_mm=`10000`, larg_mm=`1200`
- `JulioLima_2026.04.15-1.json` row 9: cliente=`ENEDIS`, modelo=`CD03P503`, comp_mm=`11000`, larg_mm=`1250`

Divergent fields:
- **modelo**: `CD03P502`, `CD03P503`
- **comp_mm**: `10000`, `11000`
- **larg_mm**: `1200`, `1250`

### OF `261902`

Appears in:
- `JulioLima_2026.04.18.json` row 3: cliente=`FEDERATION`, modelo=`CLC8F07Ri-V`, comp_mm=`7072`, larg_mm=`1410`
- `VitorCarvalho_2026.04.14.json` row 1: cliente=`FEDERATION`, modelo=`CLC8F07Ri-V`, comp_mm=`7072`, larg_mm=`1500`

Divergent fields:
- **larg_mm**: `1410`, `1500`

### OF `262109`

Appears in:
- `AugustoMonteiro_2026.04.16.json` row 4: cliente=`DAV NORDIC`, modelo=`CLC7E05D`, comp_mm=`5800`, larg_mm=`1190`
- `JulioLima_2026.04.14.json` row 2: cliente=`DAV NORDIC`, modelo=`CLC5E04D`, comp_mm=`4800`, larg_mm=`1190`

Divergent fields:
- **modelo**: `CLC5E04D`, `CLC7E05D`
- **comp_mm**: `4800`, `5800`

### OF `262120`

Appears in:
- `JulioLima_2026.04.15-1.json` row 3: cliente=`ENEDIS`, modelo=`CD03P502`, comp_mm=`10000`, larg_mm=`1200`
- `JulioLima_2026.04.15-1.json` row 8: cliente=`ENEDIS`, modelo=`CD03P503`, comp_mm=`11000`, larg_mm=`1250`
- `VitorCarvalho_2026.04.15.json` row 1: cliente=`ENEDIS`, modelo=`CD02P11B-CD02P503`, comp_mm=`11000`, larg_mm=`1100`

Divergent fields:
- **modelo**: `CD02P11B-CD02P503`, `CD03P502`, `CD03P503`
- **comp_mm**: `10000`, `11000`
- **larg_mm**: `1100`, `1200`, `1250`

