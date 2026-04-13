# CloudTest UI Regression Checklist

Bruk denne sjekklisten før merge/deploy av `CloudTest`.

## 0. Forutsetninger

1. Start appen:
```powershell
powershell -ExecutionPolicy Bypass -File .\CloudTest\start_cloud_test.ps1
```
2. Kjør hardening checks:
```powershell
powershell -ExecutionPolicy Bypass -File .\CloudTest\run_hardening_checks.ps1
```
3. Åpne `http://localhost:8601`.

## 1. Innlogging og sesjon

1. Verifiser at Microsoft-innlogging vises.
2. Logg inn med Entra og bekreft at appen laster korrekt (ikke blank side).
3. Klikk `Logg ut` og verifiser at du sendes tilbake til innlogging.
4. Logg inn på nytt og verifiser at sesjonen er stabil etter sideoppdatering.

## 2. Reporter-side

1. Opprett en ny bug med tittel, beskrivelse, kategori, alvorlighetsgrad og tildeling.
2. Kjør `Bruk AI til å fylle ut felter` med notattekst.
3. Åpne `Avanserte AI-detaljer` og verifiser at feltet viser debug/filuttrekk.
4. Kjør `Sjekk duplikater` før innsending.
5. Last opp minst ett vedlegg ved oppretting.
6. Verifiser at bug opprettes og vises i reporter-listen.
7. Åpne buggen, legg til `Ny oppdatering fra rapportør`, og lagre.
8. Verifiser at samtale og historikk oppdateres.

## 3. Assignee-side

1. Åpne en tildelt bug.
2. Klikk `AI: Foreslå løsning` og verifiser at forslag vises.
3. Klikk `Sett inn forslag` og verifiser at forslag legges inn i arbeidsnotat.
4. Kjør `Sentiment - analyse` og verifiser smiley i expander-tittel (`:-)`, `:-|`, `:-(`).
5. Oppdater status, alvorlighetsgrad, miljø, tagger og varsle-felt.
6. Last opp vedlegg på eksisterende bug og lagre.
7. Verifiser at notat publiseres i samtalen og at historikk oppdateres.
8. I sidebaren: kjør `Se etter duplikater`, test `Skjul`, og test `Slett` på en trygg testbug.

## 4. Admin-side

1. Verifiser dashboard-kort (åpne/pågår/uten ansvarlig/kritiske/negativt sentiment/inaktive).
2. Test admin-filtre i sidebaren:
   - `Opprettet fra`
   - `Sentiment`
   - `Kun uten ansvarlig`
   - `Rapportør inneholder`
   - `Tilfredshet`
3. Åpne en bug og test:
   - `Sentiment - analyse`
   - `Lukk bug` / `Gjenåpne bug`
   - redigering av kategori, tilfredshet og beskrivelse
4. Last opp vedlegg og lagre endringer.
5. Verifiser at historikk og samtale oppdateres.
6. Test `Se etter duplikater` i admin-sidebaren og verifiser `Skjul`/`Slett`.

## 5. Avsluttende røyk-test

1. Oppdater visning i sidebaren og bekreft at data vises uten feil.
2. Verifiser at ingen traceback vises i UI under normal bruk.
3. Sjekk logger:
   - `CloudTest/.runtime/logs/unified.err.log`
   - `CloudTest/.runtime/logs/unified.out.log`
4. Bekreft at appen fortsatt starter/stoppes normalt.

## 6. Godkjenning

- [ ] Hardening checks passerte
- [ ] Reporter-scenario passerte
- [ ] Assignee-scenario passerte
- [ ] Admin-scenario passerte
- [ ] Ingen blokkerende feil i logger

