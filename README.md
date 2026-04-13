# CloudTest (Unified Streamlit App)

CloudTest er en backend-los variant av bugsystemet der alt kjorer i en Streamlit-app:

- En app: `unified_app.py`
- PostgreSQL + pgvector (standardprofil i CloudTest)
- Vedlegg/lagring via backend-abstraksjon (`ATTACHMENT_STORAGE_BACKEND`, default `filesystem`)
- Microsoft Entra via Streamlit OIDC (`st.login`)

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

Starter med PostgreSQL som standard (`DATABASE_URL`), og skriver valgt DB-url ved oppstart.

Kortkommando:

```powershell
powershell -ExecutionPolicy Bypass -File .\CloudTest\start_apps.ps1
```

Valgfri SQLite fallback (kun for lokal test, ikke cloud):

```powershell
powershell -ExecutionPolicy Bypass -File .\CloudTest\start_cloud_test.ps1 -UseSqliteFallback
```

Dette setter `CLOUD_TEST_ALLOW_SQLITE_FALLBACK=true` for prosessen, slik at appen kan starte uten PostgreSQL lokalt.
Hvis du starter med `streamlit run` direkte lokalt, tillates SQLite fallback automatisk.

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

I CloudTest-modus kreves PostgreSQL som standard. For lokal dev kan du eksplisitt tillate SQLite med `CLOUD_TEST_ALLOW_SQLITE_FALLBACK=true` (settes automatisk av `-UseSqliteFallback`).
Hvis PostgreSQL mangler `vector`-extension eller brukeren ikke har rettighet til `CREATE EXTENSION`, starter appen likevel med tekst-basert fallback for embeddings.

## Sikkerhetsnotater

- Ikke commit `CloudTest/.streamlit/secrets.toml`.
- Lokal fallback-login er av som standard i CloudTest (`CLOUD_TEST_ALLOW_LOCAL_LOGIN=false`).
- Hvis du aktiverer lokal fallback-login, sett et sterkt `DEFAULT_ADMIN_PASSWORD`.
- Roter `client_secret` dersom det har blitt eksponert i lokal fil eller logg.

