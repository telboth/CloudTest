# Cloud_test UI Regression Report

Siste oppdatering: 2026-04-13 09:07:51 +02:00

## Run: 2026-04-13

### 1. Automatiske checks

- [x] `powershell -ExecutionPolicy Bypass -File .\Cloud_test\run_hardening_checks.ps1`
- [x] Compile check: OK
- [x] Smoke test: `Cloud_test parity smoke test: OK`

### 2. Manuell UI-regresjon (Reporter)

Referanse: `Cloud_test/UI_REGRESSION_CHECKLIST.md` seksjon 2.

- [ ] Opprett bug med metadata
- [ ] AI-fylling + avanserte AI-detaljer
- [ ] Duplikatsjekk før innsending
- [ ] Vedlegg ved oppretting
- [ ] Oppdatering i samtale + historikk

### 3. Manuell UI-regresjon (Assignee)

Referanse: `Cloud_test/UI_REGRESSION_CHECKLIST.md` seksjon 3.

- [ ] `AI: Foreslå løsning` + `Sett inn forslag`
- [ ] `Sentiment - analyse` viser `:-) / :-| / :-(`
- [ ] Oppdater metadata + notat
- [ ] Vedlegg på eksisterende bug
- [ ] Sidebar duplikatflyt (`Se etter duplikater`, `Skjul`, `Slett`)

### 4. Manuell UI-regresjon (Admin)

Referanse: `Cloud_test/UI_REGRESSION_CHECKLIST.md` seksjon 4.

- [ ] Dashboard-kort viser tall
- [ ] Admin-filtre fungerer
- [ ] Sentimentanalyse per bug
- [ ] Lukk/Gjenåpne bug
- [ ] Redigering av kategori/tilfredshet/beskrivelse
- [ ] Vedlegg + historikk + duplikatflyt

### 5. Funn / avvik

- Ingen automatiske feil i denne kjøringen.
- Manuelle tester ikke fullført ennå.
