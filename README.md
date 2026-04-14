# CloudTest (Unified Streamlit App)

CloudTest er en backend-los variant av bugsystemet der alt kjorer i en Streamlit-app:

- En app: `unified_app.py`
- SQLite (standard lokalt) eller PostgreSQL + pgvector
- Vedlegg/lagring via backend-abstraksjon (`ATTACHMENT_STORAGE_BACKEND`, default `filesystem`)
- Microsoft Entra via Streamlit OIDC (`st.login`)
- Soft-delete (papirkurv) med gjenoppretting i Admin
- Rolle-/policystyring for sletting og gjenåpning i Admin

## Dependencies

Standard (cloud/minimal):

```powershell
pip install -r .\CloudTest\requirements.txt
```

Valgfri lokal AI/OCR (tyngre pakker):

```powershell
pip install -r .\CloudTest\requirements-optional-local-ai.txt
```

## Start

```powershell
powershell -ExecutionPolicy Bypass -File .\CloudTest\start_cloud_test.ps1
```

Starter med `DATABASE_URL` fra miljoet hvis satt, ellers lokal SQLite som standard, og skriver valgt DB-url ved oppstart.
Skriptet kjører også `Alembic upgrade head` automatisk før appstart.
Som standard kjøres nå også schema-verifisering, og migrering-fallback er deaktivert (cutover-modus).

Kortkommando:

```powershell
powershell -ExecutionPolicy Bypass -File .\CloudTest\start_apps.ps1
```

Valgfri eksplisitt SQLite-profil:

```powershell
powershell -ExecutionPolicy Bypass -File .\CloudTest\start_cloud_test.ps1 -UseSqliteFallback
```

Dette setter `CLOUD_TEST_ALLOW_SQLITE_FALLBACK=true` for prosessen.

Valgfri dirty reindex ved oppstart:

```powershell
powershell -ExecutionPolicy Bypass -File .\CloudTest\start_cloud_test.ps1 -ReindexDirtyOnStart
```

Hvis du kun vil starte appen uten migrering (f.eks. ren UI-testing):

```powershell
powershell -ExecutionPolicy Bypass -File .\CloudTest\start_cloud_test.ps1 -SkipMigrations
```

Hvis du vil hoppe over schema-verifisering:

```powershell
powershell -ExecutionPolicy Bypass -File .\CloudTest\start_cloud_test.ps1 -SkipSchemaVerify
```

Hvis du vil tillate legacy fallback midlertidig:

```powershell
powershell -ExecutionPolicy Bypass -File .\CloudTest\start_cloud_test.ps1 -AllowMigrationFallback -EnableLegacySchemaBootstrap
```

## DB-vedlikehold (fase 4/5)

Kjør migrering manuelt:

```powershell
python .\CloudTest\scripts\db_maintenance.py --migrate
```

Verifiser schema + migreringsstatus:

```powershell
python .\CloudTest\scripts\db_verify.py --strict
```

Kjør dirty reindex manuelt:

```powershell
python .\CloudTest\scripts\db_maintenance.py --reindex --dirty-only
```

Tekst-only reindex (uten nye embeddings):

```powershell
python .\CloudTest\scripts\db_maintenance.py --reindex --dirty-only --without-embeddings
```

## Stopp

```powershell
powershell -ExecutionPolicy Bypass -File .\CloudTest\stop_cloud_test.ps1
```

## URL

- Unified app: `http://localhost:8601`

## Hardening checks

Kjor smoke-test og compile-sjekk:

```powershell
powershell -ExecutionPolicy Bypass -File .\CloudTest\run_hardening_checks.ps1
```

Dette sjekker blant annet:
- Python compile av `unified_app.py` og stottefiler
- Sentiment-mapping og symboler
- E-post normalisering/validering
- Duplikatdeteksjon
- Admin-filter parsing

## Admin drift

- **Backup/restore (SQLite)**: Opprett og last ned `.zip` med database + vedlegg, og restore ved behov.
- **Papirkurv**: Sletting flytter bugs til papirkurv. Admin kan gjenopprette eller slette permanent.
- **Ytelse**: Side-render, søkelatens og AI-ventetid vises i driftspanel/sidebar.

## UI-regresjon (manuell)

Kjor rollebasert sjekkliste for Reporter/Assignee/Admin:

- `CloudTest/UI_REGRESSION_CHECKLIST.md`
- Logg resultat per kjoring i: `CloudTest/UI_REGRESSION_REPORT.md`

## Entra (OIDC) konfig

Bruk `CloudTest/secrets.toml.example` som mal for `.streamlit/secrets.toml`.

Viktige felter:

```toml
[auth]
redirect_uri = "http://localhost:8601/oauth2callback"
cookie_secret = "long-random-secret"

[auth.microsoft]
client_id = "..."
client_secret = "..."
server_metadata_url = "https://login.microsoftonline.com/<TENANT_ID>/v2.0/.well-known/openid-configuration"
```

For Streamlit Cloud må `redirect_uri` settes til:

`https://<din-app>.streamlit.app/oauth2callback`

Legg også inn `DATABASE_URL` i secrets for ekstern PostgreSQL.
Eksempel:

`postgresql+psycopg://USER:PASSWORD@HOST:5432/DB_NAME?sslmode=require`

`ATTACHMENT_STORAGE_BACKEND` kan settes, men `filesystem` er standard og brukes av CloudTest i dag.

CloudTest støtter både SQLite og PostgreSQL. For PostgreSQL brukes pgvector når extension er tilgjengelig.
Hvis PostgreSQL mangler `vector`-extension eller brukeren ikke har rettighet til `CREATE EXTENSION`, starter appen med tekst-basert fallback for embeddings.

## Sikkerhetsnotater

- Ikke commit `CloudTest/.streamlit/secrets.toml`.
- Lokal fallback-login er av som standard i CloudTest (`CLOUD_TEST_ALLOW_LOCAL_LOGIN=false`).
- Hvis du aktiverer lokal fallback-login, sett et sterkt `DEFAULT_ADMIN_PASSWORD`.
- Roter `client_secret` dersom det har blitt eksponert i lokal fil eller logg.

