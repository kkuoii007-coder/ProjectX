# Журнал реализации

Дата: 9 ноября 2026. Среда: Ubuntu (VPS 2 CPU / 1.9 GB RAM), Python 3.12.3,
Django 5.2.19, PostgreSQL 16 + pgvector, пользователь `prdr`, проект
`/opt/philosophy-reader`, БД `philosophy_reader`.

## Этап 1 — основа (выполнено, восстановлено)

Было сделано до этого захода (Kimi k3): структура `config/`, `apps/`
(accounts, credits, library, sources, workshop, ai, operations), custom User
без email, модели кредитов и задач, seed_demo, settings dev/test/prod,
lock-файл.

Восстановлено в этом заходе:

- Миграции отсутствовали — сгенерированы и применены все (`migrate` OK).
- Циклическая зависимость library↔workshop устранена разнесением на 0001/0002.
- `sources.0002_enable_vector`: создание расширения `vector` миграцией — тестовая
  БД теперь разворачивается без ручных шагов.
- Баг `grant_welcome_credits` (`select_for_update` вне транзакции) исправлен:
  вызов безопасен и вне atomic-блока.
- Баг `create_job`: `Wallet` создаётся лениво через `get_or_create`.
- `seed_demo` был неидемпотентен (дубли ревизий и отчётов) — исправлен: поиск
  ревизии по content_hash, отчёта по (revision, hash); проверено 3 запуска.
- Исправлены URL-namespace конфликты (`accounts` vs `account`) и
  LOGIN_REDIRECT_URL.

Проверки: `check`, `migrate`, `seed_demo` ×3, HTTP smoke (health/home/read),
`createsuperuser` без email, грант-идемпотентность, идемпотентность задач,
конфликт payload, insufficient credits, жизненный цикл job (queued → running →
succeeded, списание один раз, release при ошибке).

## Этап 2 — читательский продукт (проверен)

Главная, библиотека с поиском, читалка `/read/<slug>/` с переводом предложений
по клику/Enter/Space (готовые данные, без вызовов модели), мыслители, подборки,
пустые состояния. Проверено: гостевые страницы 200, перевод рендерится из
сохранённого payload, чтение не расходует кредиты.

## Этап 3 — аккаунт (завершён в этом заходе)

- Добавлены лимиты входа/регистрации на псевдонимизированном сетевом признаке
  (HMAC с дневной ротацией, TTL ≤ 24 ч): 10/аккаунт/15 мин, 30/сеть/15 мин,
  3 регистрации/сеть/сутки, 50 глобальных/сутки.
- `RegistrationEvent` и `LoginAttempt` записываются до/вместо дорогой проверки
  пароля.
- logout-all и смена пароля отзывают сеансы: `auth_epoch` включён в
  session auth hash (`User.get_session_auth_hash`).
- Экспорт данных `GET /account/export/` (JSON attachment) — только свои данные.
- Инвайт-режим: обязательный код, одноразовое использование, блокировка
  повторного применения.
- Административное пополнение: `issue_credits --login --amount --operation-key
  --reason`, идемпотентно по operation_key, kind=topup.
- Welcome grant: ровно один (Unique(user, campaign)), начисляется в транзакции
  регистрации, 30 кредитов.

## Этап 4 — кредитный фундамент (проверен и покрыт тестами)

Wallet constraints (total≥0, reserved≥0, reserved≤total), ledger с уникальными
operation_key, резерв/списание/возврат ровно один раз, поздний результат не
списывает завершённую неудачей задачу, конфликт несовпадающего payload,
недостаток средств не оставляет резервов, lease и восстановление зависших задач.
Наборы: `tests/test_credits` (6), `tests/test_jobs` (11), `tests/test_accounts`
(8) — все проходят в изолированной тестовой БД с pgvector (`--parallel=1`).

## Этап 5 — источники и RAG (выполнен 10 ноября 2026)

- Импорт/извлечение: `apps/sources/services.py` — TXT/MD (utf-8), PDF через
  pypdf (без выполнения содержимого), нормализация (де-дефисация, абзацы,
  маркеры страниц), chunking 300–600 слов по абзацам/предложениям с overlap.
  Дедуп: повтор документа по (owner, content_hash) не создаёт новую версию.
- FTS: миграция 0003 — `search_vector` как PostgreSQL GENERATED ALWAYS
  STORED (`to_tsvector('english', text)`) + GIN-индекс. В модели — Django
  `GeneratedField` (INSERT/UPDATE не пишут колонку автоматически).
- Embeddings: `apps/sources/embeddings.py` — OpenRouter `POST /api/v1/embeddings`,
  модель `openai/text-embedding-3-small` (1536), батчи 16, проверка размерности
  и соответствия числа векторов; сеть только вне транзакций; индексация
  последовательная (память ограничена).
- Retrieval: `apps/sources/retrieval.py` — гибрид FTS (`SearchRank`, websearch)
  + pgvector cosine внутри scope (owner private ∪ editorial), нормировка ног
  по максимуму, среднее, дедуп по chunk, top-N=8. При недоступности embeddings
  FTS-нога продолжает работать; фальшивых результатов нет.
- Команды: `import_source`, `index_source`, `search_sources`.
- DocumentFetcher: `apps/sources/fetcher.py` — allowlist доменов, запрет
  loopback/private/link-local/metadata, DNS перепроверяется на каждом redirect
  (анти-rebinding), лимиты redirect/время/размер/content-type. Выключен
  по умолчанию (`WEB_FETCH_ENABLED=false`).
- Тесты: 15 (sources) + 7 (fetcher) новых; полный набор 44 — все проходят
  в изолированной тестовой БД с pgvector.
- На реальных данных: «Дневники Витгенштейна 1914–1916» (PDF, 156 КБ,
  русский текст) импортирован (42 фрагмента), embeddings записаны, гибридный
  smoke-поиск проверен на RU/EN запросах; обе ноги возвращают релевантное.
- Решение в рамках этапа: FTS-конфиг 'english' (корпус en); для RU-запросов
  semantic-нога компенсирует морфологию. Ограничение зафиксировано в README.

## Этап 6 — живой OpenRouter (выполнен 10 ноября 2026)

- Решение владельца: `OWNER_DAILY_BUDGET_USD=1`, `OWNER_MONTHLY_BUDGET_USD=30`,
  модели generation/review — `anthropic/claude-sonnet-5.5` (выбор по качеству
  перевода философского текста: лучший scholarly register в независимом
  бенчмарке академического перевода при цене $2/$10 за 1M токенов).
- `apps/ai/openrouter.py`: Chat Completions с `response_format json_schema`
  (strict), промпты из `docs/spec/prompts/`, потолок цены по конфигурации,
  учёт usage/cost в `ProviderAttempt`, бюджет-гвард до сетевого вызова
  (дневной/месячный бакеты), списание после ответа — одна `BudgetReservation`
  на job, capture в оба бакета.
- `get_provider()` в services.py: `LIVE_AI_ENABLED=true` + `AI_PROVIDER=openrouter`
  → живой провайдер; иначе FakeProvider. JobError при неполной конфигурации.
- Smoke-тест живой генерации: job → воркер → Sonnet 5.5 → ревизия сохранена
  (payload с title/вопросами/частями/предложениями en+ru), 10 кредитов
  списаны, usage/cost записаны в `ProviderAttempt`.
- В процессе найдено и исправлено: право БД на `ai_providerattempt` (миграция
  под root отняла права у роли приложения — GRANT восстановил), дублирование
  `BudgetReservation` (OneToOne на job), отступы в `fetcher.py` и
  `services.py`.
- Тесты: 44/44 (FakeProvider остаётся в dev/test; живой путь — только в prod).

## Этап 7 — мастерская правок (выполнен 10 ноября 2026)

- `apps/ai/openrouter.py`: схемы `EDIT_SENTENCE_SCHEMA`/`EDIT_PART_SCHEMA`
  (strict json_schema: 1–2 предложения замены для sentence; полный текст
  части для part), промпт editing-v2 из `docs/spec/prompts/fragment-editing.md`,
  `REVIEW_SCHEMA` + repair-v2. `AI_MAX_PROVIDER_CALLS_PER_JOB=4`.
- Сервер владеет областью правки: цель (`part` или `part:sentence`) и текст
  фрагмента передаются провайдеру из сохранённого снимка
  (`_build_edit_context`), пользовательский ввод не задаёт scope. Новый
  полный снимок собирает `apply_edit`: неизменённые предложения/переводы
  копируются дословно, заголовки и glossary sentence-правкой не трогаются,
  part-правка меняет контент только одной части. Выход за границы →
  `EditScopeError` (задача failed, резерв освобождён).
- `run_review_cycle` в services (общий для Fake и OpenRouter): review →
  (repair → свежий review), максимум 4 вызова. Исходы: pass → delivered;
  fail → `no_charge_failure` (только с сохранённым бесплатным сообщением
  kind="review"); needs_broader_edit/needs_clarification/refused → Message
  kind="explanation", без ревизии и списания.
- Head: автоматически ставится только у первой успешной серии (generate);
  правки создают ревизию без смены head. `POST /projects/{id}/head/`
  применяет ревизию (повтор — no-op, не откат). Diff ревизии против head:
  `GET /workshop/{id}/revisions/{rev}/diff/`.
- `Revision.save` enforcing immutability: изменение payload сохранённой
  ревизии → `ValueError` (старый отчёт не подтверждает новый контент).
- Формы в мастерской: правка части/предложения inline, таблица версий
  с «Сравнить»/«Применить», обсуждение (ask).

## Этап 8 — редакционная публикация и жалобы (выполнен 10 ноября 2026)

- `publish_revision` (apps/library/services.py): гейты — QualityReport для
  текущего content_hash, verified KnowledgeCard, `factual_state=supported`,
  нет blocking issues, права источников (allowed; private+unknown блокирует).
  Идемпотентно; AuditEvent(action="editorial_publish") при создании.
  `retract_publication` — отзыв с аудитом. Admin-действие публикации в
  admin ревизий с показом причин отказа (`PublicationBlocked`).
- Жалобы `POST /reports/<slug>/`: категории factual_error/too_complex/
  bad_translation/inappropriate/rights/other, лимит 3/сутки (429), 401
  без входа, одиночная жалоба не скрывает материал (spec).

## Этап 9 — экспорт и удаление аккаунта (выполнен 10 ноября 2026)

- `export_user_data` (apps/accounts/export_service.py): аккаунт, кредиты и
  транзакции, документы с текстом и чанками, проекты (ревизии, сообщения,
  jobs), избранное, слова; только свои данные, даты в isoformat.
  Management-команда `export_user_data --login [--output]` и HTTP
  `GET /account/export/`.
- `purge_user_data` (apps/accounts/purge_service.py): в одной транзакции —
  отмена queued jobs, освобождение активных резервов без списания
  (FAILED помечается error_code="account_deleted"), удаление
  RetrievalItem/ProviderAttempt, отвязка AIJob (PROTECT), удаление проектов/
  ревизий/EvidenceLink/документов/chunks/embeddings/избранного/слов/сообщений,
  LoginAttempt по логину; аккаунт деактивируется: login → `deleted-<hex>`,
  public_alias «Автор удалён», unusable password, AuditEvent(action=
  "account_purge"). Wallet и CreditTransaction сохраняются — денежный ledger
  не стирается. Команда `purge_user_data --login --reason` отказывает для
  активного staff. HTTP `POST /account/delete/` вызывает purge немедленно.

## Проверки после этапов 7–9

- Тесты: 78/78 (`manage.py test --settings=config.settings.test`):
  границы правки и неизменённые фрагменты, review/repair исходы,
  применение head, гейты публикации, жалобы, export/purge.
- Живой smoke на OpenRouter (Sonnet 5.5, пользователь smoketest):
  generate → succeeded/delivered, ревизия + head, −10 кредитов;
  edit_sentence [1:1] → заменена одна цель, соседние нетронуты, −1 кредит,
  head не тронут; edit_part (часть 3) → пример добавлен, другие части
  нетронуты, −3 кредита; diff показывает только изменённое предложение;
  apply_head → head=10, повтор — no-op.
- В процессе найдено и исправлено:
  - `BudgetReservation.job` был OneToOne при 4 платных вызовах на job —
    второй вызов (_record_cost) падал на unique constraint; миграция 0002
    переводит на ForeignKey (spec: «job/attempt»).
  - HTTP 500 на `/projects/new/` при отсутствии кошелька/кредитов: рендер
    402-страницы внутри прерванной atomic-блока ронял контекст-процессор
    `account_summary`; теперь 402 рендерится вне транзакции, проект не
    создаётся; добавлены 409 на JobConflict/QueueFull и регрессионный тест.
  - `edit_fragment` отклонял bare part index для edit_part — валидация
    разделена по видам цели.
  - Модель без контекста фрагмента отвечала needs_clarification («контекст
    пустой») — добавлен `_build_edit_context`.
- Права каталогов миграций после миграций под root проверены и исправлены
  (`chown prdr:prdr`, `u+rwX,go-rwx`) — после любого `makemigrations`/`migrate`
  под root повторять.

## Следующий этап

git init + первый коммит (проверка .gitignore на секреты); решение по nginx
и публичному доступу — за владельцем (домен/HTTPS/правовой контур).

## Эксплуатация (настроено в этом заходе)

- `/etc/philosophy-reader/service.env` (0640 root:prdr): DB_NAME/DB_USER/
  DB_PASSWORD/DJANGO_SECRET_KEY/DJANGO_SETTINGS_MODULE=config.settings.prod;
  с этапа 6 также OWNER_DAILY_BUDGET_USD/OWNER_MONTHLY_BUDGET_USD,
  LIVE_AI_ENABLED, AI_PROVIDER, AI_GENERATION_MODEL, AI_REVIEW_MODEL
  (бэкап исходного файла — `service.env.bak-20261110`).
- `/opt/philosophy-reader/.env` (0600 prdr): только OPENROUTER_API_KEY.
- systemd: `philosophy-reader-web` (Gunicorn 1 worker × 4 threads,
  127.0.0.1:8801, MemoryHigh 400M), `philosophy-reader-worker` (один
  последовательный AI-worker, MemoryHigh 300M), `philosophy-reader-maintenance`
  (oneshot) + таймер каждые 15 минут. Юнит-файлы хранятся в `deploy/`.
- Статика: `collectstatic` выполнен; для loopback-запуска без reverse proxy
  Gunicorn использует `config.wsgi_static:handler` (StaticFilesHandler).
  После согласования nginx обрабатывать статику будет прокси, wsgi_static
  удаляется.
- Проверено: `/health/` → `{"web":"ok","database":"ok","vector":"ok"}`,
  home/login/register/static 200, worker active, maintenance завершается
  успешно, Hermes не затронут (контейнер healthy, сеть 172.22.0.0/16 не
  пересекается), наружу открыт только SSH.

## Ограничения и состояние секретов

- Регистрация закрыта (`REGISTRATION_MODE=invite` по умолчанию), продажи
  выключены (`TOPUP_MODE=manual_test`). Живой ИИ включён с этапа 6
  (`LIVE_AI_ENABLED=true`, `AI_PROVIDER=openrouter`, дневной бюджет $1,
  месячный $30); FakeProvider остаётся только для dev/test.
- В `/root/philosophy-reader-spec-v1.1/.env` хранился незашифрованный
  OPENROUTER_API_KEY рядом со спецификацией; ключ перенесён в
  `/opt/philosophy-reader/.env` (0600), права на исходный файл ужесточены до
  0600. Рекомендация владельцу: считать ключ скомпрометированным при
  поступлении в открытый репозиторий пакета и заменить при первой настройке
  живого ИИ; из пакета спецификации его убрать.
- Открытый порт 8801 наружу не привязан; HTTPS/публичный доступ требуют
  отдельного решения владельца (домен, правовой контур).

## Frontend-редизайн (выполнен)

Направление «Глосса на полях»; стек: шаблоны + ручной CSS на токенах +
vanilla JS + шрифты Literata/Golos Text (OFL, self-hosted). Миграций и новых
API нет; логика views не менялась — только контекст и разметка шаблонов.

### Этап 1 (коммит f7d48aa): главная, читалка, base

- `base.html`: header с брендом-маркой ¶, мобильное меню на кнопке, лента
  notices, footer. Главная: hero со specimen-карточкой (демо-предложение с
  глоссом), featured, списки, ночная лента мастерской.
- Читалка: перевод — заметка на полях (`span.s[role=button]` +
  `span.gloss[role=note]`; справа на ≥82rem, под предложением на узких),
  reading-line, rail частей, термины, жалобы. No-JS: перевод виден, меню
  раскрыто.

### Этап 2 (коммит dd6bb72): все остальные страницы

- Библиотека: каталог (page-head + поиск + карточки), подборки, мыслители.
- Кабинет: dashboard (account-nav, пустые состояния с CTA, история задач),
  кредиты (balance-figure, секции), избранное, слова, безопасность, вход,
  регистрация, регистрация закрыта.
- Мастерская: create.html и project.html переведены на тёмную ленту
  `.workshop-page` (общий стиль с главной); страница проекта — переход с
  legacy `.sentence/.translation` на каноничную разметку `span.s`/`span.gloss`
  с `aria-controls`. Таблица версий и обсуждение — на бумажной части ниже
  ленты.
- `reader.js`: удалён `initLegacySentences` — legacy-разметки больше нет.
- `main.css`: новые секции (`.page-head`, `.search-form`, `.publication-card`,
  `.account-nav`, `.account-section`, `.balance-figure`, `.form-page`,
  `.workshop-page`, `.discourse`, `.edit-sentence/.edit-part-form`), сняты
  дубли правил. Критичный контракт сохранён: project.html безопасно рендерится
  при контексте только `{project, error}` (ошибки ask/edit_fragment) — секции
  версий и обсуждения обёрнуты в `{% empty %}`/{% if %}-защиты.

Проверки: 102 теста OK (в `tests/test_frontend.py` добавлен
`RedesignedPagesContractTests` — 7 тестов контракта новых страниц: page-head/
search на библиотеке, кабинет, кредиты, логин, глоcc-разметка проекта,
рендер ошибок без контекста). Production-деплой: коммит перенесён fast-forward
в боевую main, collectstatic (2 файла), рестарт web+worker, smoke 200 на
главной/библиотеке/читалке/справке/логине, кабинет/кредиты/безопасность и
страница проекта отдают новую разметку, POST ask валидный → 302, пустой
инструкция → 400 с корректным рендером ошибки.
Не проверено: визуальная оценка «глазами» (скриншоты сняты, не просмотрены),
реальные браузеры Safari/Firefox, скринридеры.

## Production hardening (выполнен 12 ноября 2026)

Цель — закрыть технические дефекты прод-готовности до решения владельца
о публичном доступе (nginx/HTTPS/домен остаются отложенным шагом).

- `config/settings/prod.py`: недействующий в Django 5.2
  `STATICFILES_STORAGE = ManifestStaticFilesStorage` заменён на dict
  `STORAGES` (default → FileSystemStorage, staticfiles →
  ManifestStaticFilesStorage). Добавлен явный `LOGGING` (root INFO,
  `django.request` ERROR → stderr): при DEBUG=False ошибки приложения
  теперь видны в journalctl юнитов web/worker.
- `config/wsgi_static.py`: stock `StaticFilesHandler` резолвит URL через
  finders (исходные директории) и не видит post-processed hashed-файлов в
  `STATIC_ROOT`; хендлер заменён на отдачу напрямую из `STATIC_ROOT`
  (`django.views.static.serve` с `document_root=STATIC_ROOT`). Манифест-URL
  (`css/main.<hash>.css`, шрифты, `reader.js`) отдаются корректно.
- `collectstatic --clear` с prod-настройками: 137 файлов post-processed,
  создан `staticfiles/staticfiles.json`; страницы ссылаются на hashed-URL.
- Ежедневный бэкап БД: `deploy/backup-philosophy-reader.sh` (pg_dump,
  custom-формат, 0700) → `/var/backups/philosophy-reader/`, retention 7
  дней, запуск от `postgres` (peer auth) через
  `/etc/cron.d/philosophy-reader-backup` в 03:35 UTC, лог
  `/var/log/philosophy-reader-backup.log`. Первый дамп снят и проверен
  (`pg_restore --list`: 141 объектов TABLE DATA/SEQUENCE, 568 КБ).
- Проверки: 102 теста OK; `check` и `check --deploy` (prod) — только 3
  ожидаемых HTTPS-warning (HSTS subdomains/preload, SSL_REDIRECT), они
  включаются при появлении публичного домена; рестарт web+worker, health
  `{"web":"ok","database":"ok","vector":"ok"}`; smoke 200: главная,
  библиотека, читалка, справка о кредитах, логин, admin; все hashed-статик
  ресурсы со страниц 200; ошибок в journalctl web/worker нет.
- Изменено серверное окружение вне репозитория:
  `/usr/local/sbin/backup-philosophy-reader` (копия скрипта),
  `/etc/cron.d/philosophy-reader-backup`, каталог
  `/var/backups/philosophy-reader` (postgres:postgres 0700).

## Публичный доступ и защита сервера (выполнены 12 ноября 2026)

Решение владельца: открыть сайт по IP (домена нет), «не оставив шансов».

### Аудит-предусловия

- Репозиторий `github.com/kkuoii007-coder/ProjectX` публичный (HTTP 200),
  но полная git-история обоих клонов не содержит `sk-or-` токенов;
  `.env` никогда не коммитился (gitignore работает). Утечка ключа была
  не в git, а в файле пакета спецификации на диске — ротация остаётся
  на владельце.
- До работ: парольный SSH был открыт через cloud-init override
  (`PasswordAuthentication yes`), фаервола не было (INPUT ACCEPT),
  nginx 1.24 установлен, но ничего не раздавал.

### Защита ОС

- SSH: удалён `/etc/ssh/sshd_config.d/50-cloud-init.conf`
  (PasswordAuthentication no эффективно), `PermitRootLogin prohibit-password`.
  Стабильность key-only входа подтверждена (сессии не рвутся, повторные
  подключения проходят; probes по ssh-rsa отклоняются).
- nftables (`deploy/nftables.conf` → `/etc/nftables.conf`, сервис включён):
  input policy drop, разрешены loopback, established/related, 22/80/443,
  ICMP (v4) и весь icmpv6 (NDP нужен для v6-связности), лог дропов
  5/мин. Docker-сети Hermes (172.17.0.0/16, 172.22.0.0/16) не фильтруются
  (контейнер hermes-gateway не публикует портов). Переживает restart
  и загрузку (enabled).
- fail2ban (`deploy/fail2ban-jail.local` → `/etc/fail2ban/jail.local`,
  banaction nftables): sshd aggressive (3 попытки, ban 1h, эскалация ×2
  до 1w), nginx-http-auth, nginx-botsearch, nginx-limit-req. Уже
  забанил первых сканеров SSH (121.224.5.228, 222.210.35.197 и др.).
- Проверено: наружу слушают только 22/80/443 (v4+v6); Postgres/Gunicorn/
  docker-порты — loopback/private.

### TLS и nginx

- Self-signed сертификат на 10 лет (rsa 3072, SHA-256) с SAN
  IP:185.112.100.238, IP:2a0c:5d00:2002::7e23:46b7, DNS:shults.ru
  (`deploy/openssl-san.cnf`); ключ/серт в `/etc/philosophy-reader/tls/`
  (0600/0644).
- nginx (`deploy/nginx-philosophy-reader.conf` → sites-enabled):
  80 → 301 → https; 443 ssl http2 (TLSv1.2/1.3, session cache,
  tickets off, server_tokens off, OCSP stapling off — нет responder'а
  для self-signed); proxy к 127.0.0.1:8801 с X-Forwarded-Proto;
  статика из STATIC_ROOT с immutable; limit_req 6 r/m login/register/
  admin-login и 30 r/m общий (429 при превышении); security-заголовки;
  client_max_body_size 6m; ACME-заготовка на 80 для будущего домена.
- Права: `chmod o+x /opt/philosophy-reader` — www-data получает доступ
  на чтение staticfiles через путь (каталог проекта оставлен 0750 prdr).

### Приложение

- `config/settings/prod.py`: `SECURE_PROXY_SSL_HEADER`, HSTS
  includeSubDomains+preload, `CSRF_TRUSTED_ORIGINS` (оба IP),
  `SECURE_SSL_REDIRECT` через `DJANGO_SECURE_SSL_REDIRECT=true`
  (в service.env; бэкап `service.env.bak-20261112`).
- service.env: добавлены `DJANGO_ALLOWED_HOSTS` (оба IP + loopback) и
  `DJANGO_CSRF_TRUSTED_ORIGINS`.
- Юнит web переключён на `config.wsgi:application`; `config/wsgi_static.py`
  удалён (контракт из README «после nginx» выполнен).
- CSRF-поведение подтверждено: POST с корректным IP-origin и токеном →
  200 (форма с ошибкой входа), с чужим Origin → 403.

### Review-модель Haiku 5.5

- `AI_REVIEW_MODEL=anthropic/claude-haiku-5.5` в service.env
  ($0.10/$0.50 за 1M токенов — в 20× дешевле Sonnet 5.5 $2/$10);
  цена валидирована против `AI_STAGE_PRICE_CEILINGS["review"]` (3/15).
- Живой smoke через очередь: edit_sentence и ask jobs прошли полный
  конвейер (generate/edit → review → repair → re-review), попытки
  корректно регистрируются в ProviderAttempt c моделью haiku/sonnet,
  бюджет учитывается; бюджет-гард сработал штатно при исчерпании
  дневного лимита ($1) в ходе smoke-тестов ($0.94 израсходовано за
  17 вызовов).
- Наблюдение: Haiku строже к источникам — несколько заданий закрылись
  `no_charge_failure` с бесплатным объяснением (по спеке: неподтверждённое
  не выдаётся за полезный текст). Порог настраивается владельцем при
  необходимости (вернуть Sonnet или усилить контекст карточек).

### Off-site бэкапы

- Механика: `deploy/push-offsite-backup.sh` (в `/usr/local/sbin/`)
  шифрует свежий дамп AES-256-CBC/PBKDF2 600k итераций ключом
  `/etc/philosophy-reader/backup-encryption.key` (0600, создан),
  затем rclone copy в `philosophy-backup:philosophy-reader/backups`.
- Roundtrip шифрования проверен локально: расшифрованный дамп идентичен
  оригиналу (cmp), pg_restore видит 49 TABLE DATA.
- Cron `deploy/philosophy-reader-offsite.cron` (04:05 UTC) и
  пошаговая инструкция владельцу: `deploy/README-offsite-backup.md`.
  Активация — после того как владелец создаст remote (S3/B2/SFTP)
  через `rclone config --config /etc/philosophy-reader/rclone.conf`.

### Полная верификация

- 102 теста OK; `check` и `check --deploy` — 0 предупреждений (все
  HTTPS-пункты закрыты).
- Внешний smoke: https по v4 и v6 — 200 (главная/библиотека/читалка/
  логин/статика), http → 301 → https; rate limit: 8-й POST подряд на
  login получает 429; TLS 1.1 отклоняется, TLS 1.3 работает
  (TLS_AES_256_GCM_SHA384).
- Восстановление: дамп из каталога бэкапов развёрнут в тестовую БД
  `pr_restore_test` (3 пользователя, 5 ревизий, 42 chunks) и удалена.
- Сервисы после всех переключений: web/worker/maintenance/nftables/
  fail2ban/nginx — active; память в пределах лимитов.

## Корректировки фронта и конвейера ИИ (выполнено 13 ноября 2026)

По замечаниям владельца после первых живых сессий. Приоритет явных указаний
владельца выше спеки (docs/spec/docs/ai-pipeline.md:58-56 «повторный
review=fail завершается без списания» заменён решением «пользователь всегда
получает текст»); расхождение зафиксировано здесь как выбранное толкование.

### 1. Разорванные теги шаблонов («СОДЕРЖИТ НЕДОПУСТИМУЮ ИНФОРМАЦИЮ»)

- На страницах подборки и мыслителя выводился сырой текст
  `{{ p.revision.payload.title_ru }}`. Причина: автоформатирование разорвало
  теги переносом строки в 5 шаблонах (collection, thinker, project ×3,
  credits, favorites). Парсер Django не матчит теги через `\n`
  (`tag_re` без DOTALL — официальный код Django 5.2.19, hash venv-файла
  совпадает с эталонным wheel), поэтому `{{ … }}` рендерился как текст.
- Исправление: теги склеены в одну строку во всех затронутых файлах;
  grep по `\{\{[^}]*$` чист, рендер проверен (collection/thinker/read/home
  — 200, сырых скобок нет).
- Дополнительная синхронизация клонов: в `/opt/pr-redesign` отсутствовали
  register_success.html и правки read.html/views.py/seed_demo.py от
  12 ноября — перенесены из прод-клона.

### 2. Логин в Кабинете

- dashboard.html: под заголовком добавлено «Вы вошли как **{{ user.login }}**».

### 3. Обсуждение: имена участников

- project.html: вместо технических ролей user/assistant выводятся логин
  пользователя проекта и «Магистр философии» (assistant).

### 4. Обмен моделей (бюджет)

- Генерация — Haiku 5.5 ($0.10/$0.50 за 1M), ревью — Sonnet 5.5
  ($2/$10 за 1M). service.env на сервере обновлён; дефолты
  `AI_GENERATION_MODEL`/`AI_REVIEW_MODEL` в config/settings/base.py и
  .env.example обоих клонов приведены к тому же распределению.
  Потолок цен этапов (AI_STAGE_PRICE_CEILINGS 3/15) не менялся — обе
  модели проходят валидацию.

### 5. Ревью не отклоняет текст

- Прежнее поведение: verdict=fail → no_charge_failure, пустая payload,
  пользователь не получал текст («Проверка качества не пройдена…»).
- Новое (решение владельца): run_review_cycle доставляет кандидата при
  любом исходе ревью. Repair-попытка сохранена: verdict=repair → repair →
  re-review; если повторное ревью прошло — чистая доставка, иначе —
  доставка последнего payload. execute_job больше не обнуляет payload при
  непройденном ревью; оставшиеся замечания пишутся в payload.note
  (рендерится в мастерской как предупреждение) и в QualityReport
  (review_json.verdict=fail остаётся в отчёте, eligibility_state=ineligible —
  к публикации такой черновик автоматически не попадёт).
- Промпт ревьюера дополнен: ревьюер предпочитает repair с конкретными
  issues вместо fail, для каждого blocking-issues пишет самостоятельный
  reason_ru; итоговое решение о доставке — за сервером.
- Списание: текст доставлен → кредиты списываются (delivered), даже если
  ревью нашло замечания. Free-исходы (refused/clarification/broader_edit)
  без изменений.
- Тест test_failed_review_is_no_charge_failure_with_message заменён на
  test_failed_review_still_delivers_text_with_note: outcome=delivered,
  head на новой ревизии, payload.note содержит «Замечания проверки…»,
  QualityReport verdict=fail, кошелёк 20 (списание 10 за generate).

### 6. Проверки и деплой

- 102 теста OK; `manage.py check` (prod) — 0 проблем.
- Рестарт web; smoke: /collections/first-steps/ показывает заголовок
  «Сократ: зачем спрашивать и проверять», сырых тегов нет.

## Redesign v1 verification · 2026-11-13

- Base: `main@247b81ba1932aa51caf002cb762c0b0b0d14cb0b`; isolated branch
  `design/redesign-v1`. Production and `redesign/frontend` remain untouched.
- Stage 1: repo permissions, CLI, base SHA, rulesets/hooks/workflows and routes
  checked; baseline public pages captured at 390/768/1440; detached base suite
  102/102 passed. Production browser access failed (tunnel/reset).
- Stage 2: actual Django home/reader redesign shown to owner via screenshots;
  same learning payloads, self-hosted fonts and stack. No backend/schema rewrite.
- Stage 3: catalogue/account/auth/workshop consistency, real template CSRF/gloss
  repairs, account redirect compatibility and safe favorite return paths.
- Stage 4: 132/132 Django tests, check, no migration drift, hashed collectstatic,
  45 page/viewport captures, 16 zero-violation axe scans, keyboard/forms/fake-only
  worker flows, no-JS/script-failure and long-content/reflow verification.
- Known limits: PG18 sandbox vs PG16 VPS, no production/browser cross-engine
  certification, no hosted full-app preview, prior backend purge/error-context
  issues documented separately rather than silently changing data semantics.
- Full handoff, safety, rollback and evidence: `docs/REDESIGN_V1.md`, `FLOWS.md`.
- Next stage: owner reviews PR and tests the pinned head in `/opt/pr-design`
  through SSH tunnel. No merge, production update or deployment action authorized.

## Готовый текст до публикации и выключатель вопроса · 2026-11-16

Основа: `main@4c3904a`, ветка `editorial-path-ask-switch`. Production не менялся,
платных вызовов не было. Решения владельца от 16.11.2026: карточку и текст
проверяет ИИ, владелец в admin не заходит; карточка ссылается на страницы
первоисточника; проверяющая модель — Sonnet 5.5; пометка «Written and checked by AI».

### Этап B. Путь готового текста

- Что работает: `submit_text` → `review_text` → `publish_text`; те же действия в
  admin. Текст хранится в `content/texts/`, карточка — в `content/cards/`.
  Развилка — необязательное поле `fork` части, входит в хеш ревизии.
- Новый вид задачи `review` (миграция `ai/0003`, только choices): один вызов
  проверяющей модели с текстом и проверенной карточкой, без repair и без
  пользовательских кредитов. Промпт review-v3: замечания по-русски.
- `publish_revision` не ослаблен. Добавлены отказы: verdict не pass; отчёт сделан с
  другой версией карточки; адрес не slug или занят другим текстом. Новая
  проверенная ревизия того же текста заменяет прежнюю под тем же адресом.
- Исправлено попутно: действие публикации в admin ревизий было описано, но не
  подключено (`actions`); форма «Добавить публикацию» обходила проверку; расход на
  review считался по ценам генерирующей модели; адрес из заголовка с «?» ронял
  главную; `revision.pk.hex` падал при пустом заголовке.
- `seed_demo` ничего не публикует. Снятие Сократа на сервере —
  `withdraw_publication socrates-questions`.
- Карточка `plato-laches-courage` v1: 10 положений с разделами 190d–192c и 199e,
  текст и номера разделов сверены ИИ с переводом У. Лэмба (Perseus). Человек не
  вычитывал.

### Этап C. Вопрос по тексту выключен

- `ASK_ENABLED=false` по умолчанию. Отказ до создания задачи и резерва: во
  view (ответ 403 со страницей проекта) и в `create_job` (`AskDisabled`).
- Вместо формы строка «Questions are not ready yet. You can still read your
  text.» с русской подписью. Справка о кредитах не показывает цену вопроса.
- Дефект пустого ответа не исправлялся; воспроизводится тестом с expectedFailure.

### Проверки

- 173 теста: OK, из них 1 ожидаемый провал (дефект ask). `check` без замечаний,
  дрейфа миграций нет.
- Локально (PostgreSQL 18 + pgvector, AI_PROVIDER=fake): команды пройдены вручную;
  тестовый провайдер даёт unverified и публикацию не открывает.
- Скриншоты мастерской 390 и 1440 px до и после.

### Ограничения

- Живой вызов проверяющей модели не выполнялся: ответ Sonnet 5.5 на review-v3 и
  совместимость строгой JSON-схемы с этим endpoint не проверены.
- Читалка пока не показывает развилку и блок «About this text» (этап F).
- Сервер не обновлён; текст о Сократе на нём всё ещё опубликован.

### Исправления после независимого ревью

Ревью нашло обходы проверки публикации; закрыты до слияния:

- `QualityReport` в admin стал только для чтения (раньше отчёт можно было
  создать или поправить формой и тем самым открыть публикацию).
- Статус `Publication` в форме не редактируется (раньше снятый текст можно было
  вернуть формой без проверки).
- Проверка публикации требует `verdict=pass`, `eligibility_state=eligible` и
  непустой список версий карточек в отчёте, совпадающий с карточками ревизии.
  Отчёт цикла генерации (без карточек) публикацию не открывает.
- Ответ проверяющей модели неверной структуры даёт `verdict=fail` и блокирующее
  `malformed_review`; неизвестная severity считается блокирующей; непустые
  `unsupported_claims` лишают допуска; блокирующие замечания не обрезаются
  ограничением в 20 записей.
- У текста один адрес: публикация под другим slug отклоняется
  (`slug_mismatch`), `publish_text` берёт адрес прежней публикации.
- Повторная подача того же текста с другой версией карточки заменяет связь
  (аудит `editorial_cards_changed`), прежний отчёт перестаёт подходить.
- Логин `editorial` зарезервирован; служебная учётка обязана быть неактивной и
  без пароля. Содержимое существующей карточки в admin не редактируется.
- Воркер не отвечает на вопрос, поставленный в очередь до выключения
  (`ask_disabled`, резерв возвращается). Оплаченный вызов записывается в
  `ProviderAttempt` и при сбое разбора ответа.
- Источник карточки: адрес раздела 190e на Perseus вместо общей страницы.

Оставлено как известные ограничения, поведение прежнее: `review_text --inline`
может тратить бюджет одновременно с воркером; вызов с пустым usage не попадает
в учёт расходов; аренда задачи не проверяется при её завершении; задача review
не учитывается в общем пределе очереди; откат на прежнюю проверенную ревизию
проходит проверку (так задумано).

Тесты после исправлений: 193, OK, 1 ожидаемый провал (дефект ask).

Этап D выполнен (см. ниже).

## 16 ноября 2026. Этапы D, E, F и единый дизайн

Этап D: владельцу показаны два статичных варианта первого экрана. Он выбрал
вариант A «Поле», велел оставить подсказку `Tap a sentence for Russian.` и
привести к этому дизайну все остальные страницы. Снимки: `docs/stage-bcd/`.

### Этап E. Главная и библиотека

- Главная — обложка стартового чтения: английский вопрос на цветном поле, два
  первых предложения с переводом по касанию, одна кнопка `Start reading`,
  малая строка. Регистрации, кредитов, генератора и поиска на ней нет.
- Стартовое чтение задаёт `START_READING_SLUG`; запасного «последнего
  опубликованного» нет: если текст не опубликован, обложка говорит
  `The first question is coming soon.`
- Один текст показан один раз. Пока он один, вторая полоса —
  `More questions soon.`; остальные тексты идут в ней списком вопросов.
- Тон обложки хранится в `Publication.cover_tone` (миграция `library/0003`) и
  берётся из поля `cover` файла текста, вне payload.
- Библиотека, страницы мыслителя и подборки: вопросы карточками своего тона,
  английская строка с русской подписью. Поиск показан от 6 текстов.
- Форма мастерской перенесена с главной на `/projects/new/`.

### Этап F. Читалка

- Текст начинается на первом экране; каждое предложение на своей строке;
  перевод открывается под строкой и скрывается повторным касанием; постоянного
  подчёркивания и заметок на полях нет.
- Развилка после части: вопрос, кнопки `Yes` / `No` / `Not sure`, одна
  подготовленная строка на выбор и переход к следующей части. Это не форма:
  ничего не отправляется, не сохраняется и не оценивается.
- В конце: слова, `About this text` с источником и пометкой
  `Written and checked by AI.`, `Next question soon.` и одна ссылка
  `Write to us` (`CONTACT_EMAIL`). Гостю больше не предлагают войти ради жалобы.
- Переключатель `RU` скрывает русские подписи интерфейса; выбор хранится в
  браузере. Без JavaScript переводы видны, развилка — обычные абзацы.

### Единый дизайн

- Все страницы открываются цветным полем с шапкой, крупным заголовком и
  линией-развилкой; содержимое — тёмным по бумаге. Прежний акцент и тёмная
  полоса мастерской убраны. Служебные страницы используют тон clay.
- `AGENTS.md` и спецификация (версия 1.4) обновлены: палитра вместо одного
  акцента, язык пути чтения, правило публикации готовых текстов.
- Попутно: список избранного показывает только опубликованные тексты.

### Проверки

- Тесты: 232, OK, 1 ожидаемый провал (дефект ask). `check` без замечаний,
  дрейфа миграций нет.
- Chromium (Playwright), ширины 320, 390, 768, 1440: горизонтальной прокрутки
  нет, консоль пуста, внешних запросов нет; первый экран главной и читалки
  измерен; контраст всех пар не ниже 4,5:1; клавиатура, режим без JS и отказ
  скрипта проверены. Снимки до и после: `docs/stage-ef/`.

### Ограничения

- Проверено только в Chromium: Safari, Firefox, реальные телефоны и экранные
  дикторы не проверялись.
- На экранах 320×568 и 360×640 очень длинный вопрос вместе с двумя длинными
  первыми предложениями сдвигает кнопку ниже первого экрана; с первым текстом
  всё помещается.
- Живой вызов проверяющей модели не выполнялся; сервер не обновлён.
- Дефект вопроса по тексту не исправлялся (выключен).

Следующий этап: G — доверенный сертификат, только с разрешения владельца.

## 2026-11-18. Диагностика читалки на живом сервере (без правок кода)

Задача: выяснить, почему на живом сайте не видно русскую подсказку и развилку.

### Что проверено

- Развёрнут commit `563ede3` (main), рабочее дерево чистое; миграции применены
  (включая `library/0003_publication_cover_tone`).
- Сервисы `philosophy-reader-web` (gunicorn) и `philosophy-reader-worker`
  активны; `/health/` отвечает; ошибок в журналах за 2 дня нет.
- Статика на живом сайте побайтово совпадает с репозиторием
  (`static/js/reader.js`, `static/css/main.css`).
- На живом сайте опубликован только `socrates-questions` (ревизия 12,
  3 части, без развилки). `START_READING_SLUG` указывает на
  `what-does-it-mean-to-be-brave`, но этой публикации нет → на главной
  «The first question is coming soon», в читалке развилки нет.
- Текст «What does it mean to be brave?» (ревизия 17) прошёл `submit_text`,
  но не опубликован: отчёт качества №10 — `verdict: repair`, 6 замечаний,
  среди них блокирующее: Сократу приписывается реплика «These men are
  brave too» — ловушка из карточки `socrates-questions`.
- Код читалки (`templates/library/read.html`, `static/js/reader.js`) не менялся:
  причина не в нём.

### Причина

Первый текст не опубликован: проверяющая модель дала блокирующее замечание
к ревизии 17, и `publish_revision` корректно не пропустил текст. Читалка
и её код исправны; на живом сайте просто нет опубликованного текста с
развилкой.

### Решение

По правилам задачи текст не правим. Блокирующее замечание принесено
владельцу: для публикации нужно исправить авторство реплики в тексте
(или снять замечание), затем повторить `review_text` → `publish_text`.
Бюджетных вызовов на это не тратили.

### Не проверено

- Скриншоты живого сайта не снимались (по решению владельца на этот прогон).
- Повторная живая проверка модели после правки текста не выполнялась.

## 2026-11-18. Этап «Полка»: «Политика» Аристотеля (в работе)

Задача владельца от 18.11.2026: перевод «Политики» (H. Rackham, Loeb 264)
на полке и в редакционном корпусе; текст «On the shelf: Aristotle, Politics»
в библиотеке; мастерская без NIETZSCHE-placeholder. Ветка
`kkuoii007-coder/shelf-stage`.

### Сделано

- Команда `import_editorial_source` (apps/sources/...): импорт редакционного
  источника без пользовательских лимитов, метаданные work/edition, дедуп
  по content_hash; тесты `EditorialImportTests` (tests/test_sources.py, 19 OK).
- Извлечение английского перевода из PDF (728 стр., Loeb 1932): тело страниц,
  Bekker-локаторы восстановлены по заголовкам страниц и монотонной
  последовательности Bekker-токенов с греческих разворотов. Результат:
  Bekker 1252–1342, все 8 книг, ~104.5k слов; файл `/tmp/politics_extracted.txt`
  (не в git). Ограничения: OCR-джиттер порядка слов; полоса краевых ключевых
  слов (~2494 слов) не вырезается — нет чистой x-границы.
- Импорт в прод-БД: SourceDocument `89c6d412-a9d9-4ad2-8496-723ba8bfc284`
  (owner=None, editorial). Расход: 312 embedding-вызовов (index_source).
- Карточка знаний `content/cards/aristotle-politics-city-v1.json` (6 фактов,
  8 ловушек, verified/ai, 2026-11-18): положения сверены ИИ с извлечённым
  переводом (1252b–1253a, 1275a, 1287a, 1296b); человек не вычитывал.
- Текст `content/texts/why-do-people-live-in-cities.json` («On the shelf:
  Aristotle, Politics», cover=sea, 5 частей, развилка в 1-й части, 402 англ.
  слова): подан `submit_text` → ревизия 18, карточка загружена verified.
- Живая проверка ревизии 18 (отчёт 11, Sonnet 5.5, 1 платный вызов):
  verdict=repair, factual_state=mixed. Три блокирующих замечания: спор
  с «торговой» концепцией города (ч.1), обоснования превосходства среднего
  слоя (ч.5), статья glossary «nature». Плюс 8 неблокирующих и 6 «нет опоры».
  Владелец решил править (решение от 18.11.2026).
- Мастерская `create.html`: Nietzsche-placeholder заменён; placeholder и
  ротация трёх примеров «On the shelf: …» (Politics, Plato the cave,
  Socrates) — `TOPIC_EXAMPLES` в views, ротация в `reader.js`
  (`initTopicExamples`, пауза при `prefers-reduced-motion`), стиль
  `.workshop-examples` в main.css. Без JS — первый пример в placeholder.

### Проверки

- Полный тест-набор: 253 теста, OK (1 ожидаемый провал); check без замечаний.
- Retrieval на проде (search_sources --login owner): «law is reason without
  desire» → combined 1.0 ( ord=126); «middle class best administered» → 1.0;
  «citizen defined judicial functions office» → 0.989.
- Рендер `/projects/new/` на проде (test client, secure): 200, Ницше нет,
  три примера и placeholder корректны; web-сервис перезапущен, /health/ 200.

### Ограничения и открытые вопросы

- locator_json чанков остаётся `{}` (retrieval.py:36); Bekker-маркеры
  сохранены в тексте чанков, отдельной структурой не прокинуты.

### Итерации по замечаниям review (решение владельца: правим)

- Ревизия 19 (карточка v2, 14 фактов): спор о торговле опёр на факт
  not-merely-trade (1280b–1281a, этруски и карфагеняне), обоснования среднего
  слоя — на middle-class-reasons (1296b), glossary «nature» переформулирован.
  Отчёт 12 (2-й платный вызов): verdict=repair; блокирующие — лишняя фраза
  «люди не появились раньше городов» и перевод «в сообщении».
- Ревизия 20 (карточка v3: + факт nature-as-end 1252b30–1253a1): правка по
  отчёту 12. Отчёт 13 (3-й платный вызов): verdict=repair; 4 блокирующих
  формулировочных (fork «люди его создают», «is made for life», glossary
  «not by plan», «Reason»/«wisdom»).
- Ревизия 21 (правка по отчёту 13, карточка v3 без изменений): fork без
  приписываний, «comes into existence for the sake of life» дословно,
  wisdom/reason согласованы, подлежащие и модальности выправлены.
  Отчёт 14 (4-й платный вызов): **verdict=pass, factual_state=supported**.
- Публикация `publish_text --admin owner`: **/read/why-do-people-live-in-cities/**
  (cover=sea, 5 частей, развилка, 459 англ. слов). Ревизии 17–20 остались
  неопубликованными (иммутабельность, гейт сравнивает card_versions_json).
- Рендер `/projects/new/` на проде (test client, secure): 200, Ницше нет,
  три примера и placeholder корректны; web-сервис перезапущен, /health/ 200.

### Расход этапа (итог)

- 312 embedding-вызовов (index_source).
- 4 живых review-вызова по «Политике» (отчёты 11–14, до pass).
- Остались разрешены: десять контрольных вопросов (этап 4).

Следующий этап: этап 3 — генерация с источниками (retrieval в execute_job),
этап 4 — контрольные вопросы.

## 19 ноября 2026. Раздел 1: развилка после каждой части и словарный гейт

Выполнено по промпту «развилки и ответы с источником» (раздел 1). Ветка
`fork-per-part` → PR #4 → main (merge cb4b733); скриншоты PR #5 → main (d26c0d6).

### Backend

- `check_editorial_payload` (`apps/library/editorial.py`): после каждой
  непоследней части требуется развилка; подписи кнопок — ровно
  `Yes`/`No`/`Not sure` (FORK_LABELS); вопрос развилки обязан кончаться «?».
  Опубликованные ревизии не переконтролируются (гейт только при подаче).
- Словарный профиль по списку CEFR-J Vocabulary Profile 1.5
  (`content/wordlists/cefrj-vocabulary-profile-1.5.csv`, 7799 строк; README
  с условиями: бесплатное использование с указанием авторства — Yukio Tono,
  TUFS). Уровень слова — минимальный среди частей речи; имена собственные
  (строки с заглавной) не считаются. Команда
  `manage.py vocabulary_profile <файл>` печатает доли по уровням, слова
  выше A1 (с пометкой «в словарике»), слова выше A2 без словарика и три
  самых длинных предложения.
- Гейты `submit_text` (контроль, не обещание уровня A1): доля A1 ≥ 90 %,
  каждое слово выше A2 — в словарике текста, предложение ≤ 16 слов.
- Поиск словоформ: `-s/-es/-ies/-ed/-ing/-er/-est`, `'s`, `n't` плюс карта
  нерегулярных форм (men→man, went→go, said→say, fought→fight…). Слова
  из глоссария не исключаются из счётчика — глоссарий прощает только
  превышение A2, уровень в профиле остаётся честным.

### Frontend

- `read.html`: развилки с `data-fork-id`/`data-fork-next`, кнопки с
  `data-fork-key`, подсказка «Choose an answer to read on. Any answer is
  fine. / Выберите ответ, чтобы читать дальше. Подойдёт любой.» между
  кнопками и вариантами ответов.
- `reader.js`: выбор сохраняется в `localStorage` (`pr-fork-choices`), часть
  после развилки скрыта (`is-locked`) до выбора, любой из трёх ответов
  открывает следующую часть, строка ответа получает фокус; при загрузке
  восстановленный выбор переоткрывает части. Выбор можно поменять, открытое
  не прячется. На сервер ничего не уходит; оценок и счётчиков нет.
- Без JavaScript и при печати виден весь текст; развилка — абзацы.
- Якорь `#part-N` открывает текст до части N (выбор снимает блокировку).

### Тексты

- `what-does-it-mean-to-be-brave.json` перенесён дословно из приложения А
  (сверка программой: 38 предложений и 3 развилки совпадают); правка по
  замечанию отчёта №9: реплика «These men are brave too» убрана (Сократ
  причисляет к смелым и тех, и других, 191d–e), первая реплика части 2
  уточнена, добавлены развилки после частей 2 и 3. Части: 10/11/12/5
  предложений, 3 развилки.
- Профиль: 247 слов, 93.9 % A1; выше A1 — brave(A2, x12), soldier(A2, x1),
  soldiers(A2, x2), все в словарике; максимум 12 слов. Хеш FIRST_TEXT_HASH
  в тестах обновлён.

### Проверки

- Полный тест-набор: 262 теста OK (1 ожидаемый провал — дефект ask).
  Новые: ForkRulesTests (3), VocabularyGatesTests (6), тест подсказки и
  открытия части в reading.
- Деплой: бэкап БД (db-20261119-172129.dump), checkout main, migrate (нет
  новых), collectstatic, рестарт web+worker, `/health/` → web/database/
  vector ok; смоук 200 на /, /library/, /read/why-do-people-live-in-cities/.
- Живой сайт (390 и 1440 px): до выбора видны часть 1 и вопрос; после
  выбора — строка ответа и часть 2; после перезагрузки открытые части
  остаются открытыми; якорь #part-3 открывает текст; без JS весь текст;
  консоль пуста; первая строка части 1 видна на 390×844 без прокрутки.
  Скриншоты в `docs/screenshots/section-1/` (11 файлов).
  (20 ноября 2026: эти снимки удалены и заменены папкой `docs/screenshots/pr1-fork/` — они были сняты на старом тексте с одной развилкой, а шаги 2–4 были одним и тем же файлом.)

## 19 ноября 2026. Раздел 2 (начало): два текста библиотеки

Ветка `section2-library-texts`. Сделано до публикаций:

- `content/texts/why-do-people-live-in-cities.json` создан дословно по
  приложению Б: 4 части (10/12/11/12 предложений), развилки после частей
  1–3, обложка sea, источник «Aristotle, Politics, translated by
  H. Rackham (1932)», авторство ai, словарик 6 слов (city, man,
  political animal, voice, part, god), simplification_note_ru из промпта.
  Перенос сверён программой (все предложения, заголовки, вопросы и ответы
  развилок совпадают с таблицами промпта).
- Карточка `aristotle-politics-city` версии 4
  (`content/cards/aristotle-politics-city-v4.json`): 19 фактов, 12 ловушек.
  Места всех фактов сверены с разметкой Perseus
  (tlg0086.tlg035.perseus-eng2.xml, CC BY-SA 4.0): каждая страница Беккера
  подтверждена дословной фразой Рэкхема на этой странице. Исправлены места
  v3: этруски и карфагеняне — 1280a (не 1280b31–1281a3); довод о богатых
  и бедных — 1295b (не 1296b); свобода от распрей и лучшие законодатели —
  1296a (не 1296b). Добавлены опоры текста Б (голос и слова, общие
  представления, «не важно, каковы люди», «настоящий город — добродетель»).
  Локаторы — книга+страница Беккера (вид `Politics 1.1253a`), номера строк
  внутри страниц не сверены и не названы.
- Словарный профиль brave после правок (см. ниже): 256 слов, 93.4 % A1,
  выше A1 — brave/soldier/soldiers (A2, в словарике), максимум 12 слов.
- Профиль текста Б: 311 слов, 97.7 % A1; выше A1 — god(B2), part(A2),
  political(A2), voice(A2), все в словарике; максимум 13 слов. В промпте
  заявлено 98 % A1; расхождение 0.3 п.п. объясняется тремя уточнениями
  поиска (ниже).
- Уточнения `profile_vocabulary` (тесты добавлены): словоформа берёт
  минимальный уровень среди своих базовых слов («means» — глагол mean A1,
  а не сущ. B2); «wrote»→write добавлен в нерегулярные; слово только в
  заглавной записи не в начале предложения считается именем собственным
  («Politics» — название книги), а слово из многословного термина глоссария
  считается глоссированным («political» из «political animal»).
- Правки brave по замечаниям отчёта №10 (pk=10, ревизия 17; меняется только
  названная строка, оговорки — в примечание): ч.2 предл.4 «People say: in
  that war…» (оговое замечание о «говорят»); ч.1 предл.4 «He is a soldier,
  and a soldier knows war.» (замечание о неподтверждённом «He knows war
  well»); ответ yes развилки 1 без раскрытия хода аргумента («You say yes.
  Read on and see.»); ч.3 предл.1 «Socrates does not tell Laches that he
  is wrong.» (замечание об авторской трактовке). Профиль после правок
  проходит гейты; новый FIRST_TEXT_HASH
  787111c0cc57b9ea90677c805dd806a7ec5428ea6bf2c3ead30734a8fb7f7c78.
- Полный тест-набор: 266 тестов OK (1 ожидаемый провал).

Дальше по разделу 2: словарный профиль → submit_text → review_text
(≤2 платных проверки на текст) → publish_text для brave и cities;
проверка обложки главной, библиотеки и адреса /read/why-do-people-live-in-cities/.

## 20 ноября 2026. Раздел 2 (завершение): публикация обоих текстов

Порядок по правилу владельца: бесплатная доводка по уже оплаченным
отчётам, затем ровно одна платная проверка на текст; при вердикте без
допуска — стоп и доклад, автоматических повторов нет. Строгий гейт
публикации не менялся и не ослаблялся.

Доводка перед проверками (коммит 4475e2e, меняется только названная
строка; оговорки — в примечание о упрощениях):

- Остаток отчёта №15 по brave (одно необработанное утверждение):
  «Two men talk in Athens.» — место действия не в карточке — заменено
  на «Two men talk.»; упоминание Афин убрано и из примечания. Остальные
  замечания №15 (время глаголов, глоссарий, финальный вопрос) были
  отработаны раньше (коммит 6a4175f).
- Отчёт №16 по тексту Б: утверждение «Aristotle lived in Greece a long
  time ago.» (биография не в карточке) заменено на «Aristotle thought
  about the city.»; двусмысленность «Тогда город создаёт торговля»
  исправлена на «Тогда торговля создаёт город»; три замечания рецензент
  прямо разрешил закрыть примечанием — природа против времени, гипотетический
  пример торговых городов, упрощение «нужны другие люди» — дописаны в
  simplification_note_ru.

Гейты и проверки:

- Словарный профиль brave (248 слов): 94.0 % A1; выше A1 —
  brave/soldier/soldiers (A2, все в словарике); максимум 12 слов.
- Словарный профиль текста Б (308 слов): 97.7 % A1; выше A1 —
  god(B2, в словарике), part/political/voice (A2, в словарике);
  максимум 13 слов. Блокирующих слов нет у обоих.
- submit_text: brave — ревизия 25 (hash cee91034…), текст Б — ревизия 26
  (hash d533bf30…).
- review_text (по одному платному вызову, anthropic/claude-sonnet-5.5):
  отчёт №17 (brave, rev 25) — verdict=pass, factual=supported, eligible,
  0 blocking, 4 nonblocking, 0 неподтверждённых утверждений; отчёт №18
  (текст Б, rev 26) — verdict=pass, factual=supported, eligible,
  0 blocking, 4 nonblocking, 0 неподтверждённых утверждений. Оба отчёта
  допустили публикацию с первой попытки.
- publish_text: оба текста опубликованы (админ owner) под прежними
  адресами: /read/what-does-it-mean-to-be-brave/ (ревизия 25) и
  /read/why-do-people-live-in-cities/ (ревизия 26). Расход платных
  вызовов за публикацию: 2 (по одному на текст).

Остальные пять замечаний отчёта №10 (pk=10, дословно) и их судьба в
опубликованном тексте:

1. [blocking] pitfall_socrates_calls_men_brave (часть 2, предложение 8): Фраза «These men are brave too» подана как прямая речь Сократа. В источнике (191d–е) он не произносит такого отдельного суждения о бегущих и возвращающихся воинах. Он включает сражающихся с отступлением в число смелых, и Лахет соглашается. Это прямо указано в карточке как ловушка. Примечание заявляет, что это пересказ, а не цитаты, но кавычки и «Socrates says» читаются как цитата.
   Предложение правки: Убрать прямую речь. Передать как вопрос Сократа или как включение таких людей в число смелых, с согласием Лахета. Сохранить связь с 191d–е.
   — Отработано: реплика убрана, ч.2 открывается «Socrates says: "Yes, a man who stays is brave."».
2. [nonblocking] plataea_framing_lost (часть 2, предложение 4): Пример со спартанцами при Платеях в источнике вводится словами «говорят». Здесь он стал безличным утверждением «In this war, some soldiers run away first», и это похоже на историческое сообщение. Карточка предупреждает, что это пример внутри аргумента, а не история битвы.
   Предложение правки: Добавить оговорку вроде «people say» или пояснить, что это пример в споре, а не рассказ об истории.
   — Отработано: «People say: in that war, some soldiers run away first.»
3. [nonblocking] unsupported_background (часть 1, предложение 4): «He knows war well» и место действия «in Athens» в карточках не подтверждены. Лахет в тексте — полководец, и это частично оговорено в примечании.
   Предложение правки: Убрать или смягчить эти детали, либо добавить их в примечание о упрощениях.
   — Отработано: «He knows war well.» остался и объявлен авторским пояснением в примечании; упоминание Афин снято при доводке по отчёту №15 (см. выше).
4. [nonblocking] glossary_definition_and_fight (часть glossary, предложение None): Определение «A brave person is not afraid to do hard things» даёт готовое определение смелости. Диалог же заканчивается без определения, а ни один из ответов не принят как итог. Объяснение «People fight when they want to hurt each other» плохо подходит к контексту войны и боя.
   Предложение правки: Дать нейтральное пояснение «brave», не претендующее на определение. Для «fight» взять значение «use force against an enemy in a battle».
   — Отработано: brave — «A person who does something hard or dangerous.», fight — «To be in a battle against an enemy.»
5. [nonblocking] fork_gives_away (часть fork, предложение None): В ответе «yes» фраза «Then the first answer has a problem» заранее раскрывает ход аргумента и снижает ценность выбора читателя. Ответ «no» связывает позицию читателя с первым ответом Лахета лишь по сходству, это допустимо, но неточно.
   Предложение правки: Переформулировать реплики нейтрально, не раскрывая вывод.
   — Отработано: «You say yes. Read on and see.» и нейтральные реплики развилок.
6. [nonblocking] negation_not_in_source (часть 3, предложение 1): Утверждение «Socrates does not say: “You are wrong.”» не опирается на факты карточек. Это авторская трактовка, хотя фраза «my question was not good» поддержана (190e, 191c). Фраза «This time it is not easy» — тоже оценка составителя.
   Предложение правки: Оставить как комментарий составителя или опереться на факт, что Сократ винит свой вопрос.
   — Отработано по варианту «комментарий составителя»: строка объявлена в примечании; «This time it is not easy» отмечена рецензентом №17 как мелкое допущение, предложение правки — примечание (внести при следующей правке текста).

Проверки готовности (пункт 71–75 промпта):

1. Обложка главной ведёт на brave: START_READING_SLUG =
   what-does-it-mean-to-be-brave, на / заголовок «What does it mean to
   be brave?». Выполнено.
2. В библиотеке оба текста; /read/why-do-people-live-in-cities/ открывает
   новый текст («Aristotle thought about the city.» на месте).
   /read/what-does-it-mean-to-be-brave/ открывает brave с тремя
   развилками (data-fork-id ×3), первая строка «Two men talk.».
3. Выводы словарных профилей и вердикты проверок — в этом разделе выше.

Тесты: 266 OK (1 ожидаемый провал); FIRST_TEXT_HASH обновлён на
cee91034107e66c1ad5f37d9861641eea2138d1e2946c0a655ce3914bc74b815.
Ветка section2-publication, коммит 4475e2e, смержен в main (fast-forward).
Прод: бэкап БД перед публикациями не требовался (публикации добавляют
записи, миграций нет; последний бэкап db-20261119-172129.dump).

Дальше по плану: раздел 3 — полка (сравнение PDF-пути и Perseus-XML,
команда извлечения+загрузки, locator_json, список полки, атрибуция,
одна переиндексация, замеры).

## 20 ноября 2026. PR 1, часть «Развилка»: текст открывается по шагам

Дефекты на `main@da6987c` (подтверждены локально сценарием в браузере): при
открытии видны первая часть и сразу все три вопроса; ответ на третий вопрос
открывал часть 4 при скрытых частях 2 и 3; ответы хранились под общим ключом
`pr-fork-choices` и переходили в другой текст; ссылка `#part-3` на чистом
профиле ничего не открывала; подсказка под кнопками была набрана `#65665e`
по цвету обложки (контраст 1,33 / 1,59 / 1,36 на clay / sea / olive).

### Что сделано

- Модель — цепочка. Состояние `{answers, upto}`: ответы по развилкам и число
  открытых частей. Правило одно (`advance()`): следующая часть открыта, если
  между частями нет развилки или на неё дан ответ. Одна функция `render()`
  расставляет видимость всей страницы (части, развилки, строки ответов,
  подсказка, «Read on», `About this text`, `Next question`); точечных снятий
  классов больше нет. `Words` и блок действий вошедшего читателя видны всегда.
- Хранение — только на устройстве, ключ `pr-fork:<slug>:<первые 16 знаков
  content_hash ревизии>`. Имя отдаёт сервер: `fork_scope()` в
  `apps/library/presenters.py` → `data-fork-scope` на `article.reader` (только
  у текста с развилками; текст без развилок никогда не закрывается). Хеш, а
  не pk ревизии: он неизменяем и не выдаёт внутренних номеров. При загрузке
  удаляются ключи того же адреса с другой ревизией и старый общий
  `pr-fork-choices` (он больше не читается). Без `localStorage` состояние
  живёт в памяти страницы.
- Открытое не закрывается; ответ можно сменить. Ответ на развилку, до которой
  читатель не дошёл, из хранилища не принимается.
- `#part-N` (при загрузке и при `hashchange`) открывает текст до части N, не
  выбирая ответов: пройденные развилки показаны с кнопками и «Read on», без
  строки ответа. После раскрытия страница прокручивается к части и фокус
  переходит на неё. При перезагрузке скрипт страницу не двигает: место
  выбирает браузер (без якоря в адресе — прежнее, проверено).
- Скрытие — в CSS, в одном блоке `@media screen` под `.js`: до запуска
  скрипта открыты первая часть и первая развилка, вспышки всего текста нет;
  без JavaScript, при сбое скрипта (откат по `data-js-ready`) и при печати
  виден весь текст со строками всех ответов. Скрытое — `display: none`.
- Подсказка: цвет текста обложки (`--on-cover`, `#f7f5ef`) и та же строка
  интерфейса, что у подсказки в шапке (`500 var(--fs-1)`); палитра не менялась.
  Контраст (посчитан в браузере по фактическим цветам): clay 7,10, sea 8,47,
  olive 7,27. Подсказка показывается скриптом вместе с кнопками и только у
  развилки, после которой есть часть; после прохождения развилки скрыта.
- На сервер ничего не отправляется, ничего не считается.

### Проверки

- Тесты: 278 OK (1 ожидаемый провал). Новые и обновлённые проверки в
  `tests/test_reading_path.py` (имя места чтения = адрес + ревизия, новая
  ревизия — новое имя, текст без развилок без имени, серверный HTML без
  скрывающих атрибутов и классов, контракты `reader.js` и `main.css`:
  один `render()`, старый ключ только удаляется, `hashchange`, скрытие только
  в `@media screen`, печать ничего из текста не прячет) и
  `tests/test_redesign_contracts.py` (`ForkedTextWithoutScriptTests`, разбор
  HTML). Двенадцать пробных поломок кода этими тестами пойманы.
- Браузерный сценарий `tools/qa/fork_steps.js` (Playwright, Chromium; в набор
  тестов и в CI не входит): 132 утверждения на 390 и 1440 px, все выполнены.
  Шаги: первое открытие, ответы 1–3 (третий с клавиатуры), перезагрузка,
  второй текст после прочтения первого, новая ревизия (настоящая
  перепубликация временной копии и подложенное состояние другой ревизии),
  `#part-3` на чистом профиле и `hashchange`, контраст подсказки на трёх
  тонах, без JavaScript, сбой загрузки `reader.js`, печать при включённом
  JavaScript, испорченное хранилище, отсутствие `localStorage`, 320 px,
  вошедший читатель. Печать в PDF из закрытого состояния: 5 страниц, все
  четыре части, строки всех ответов и концевые блоки на месте.
- Локальная приёмка шла на отдельной базе с двумя текстами, опубликованными
  настоящим редакционным путём с заглушкой проверяющего; тон olive и новая
  ревизия — временная копия текста в `/tmp`, после съёмки снята с публикации.
  Платных вызовов не было. Заглушка и копии в репозиторий не входят.
- Снимки: `docs/screenshots/pr1-fork/` (28 файлов, 390 и 1440 px, шаги 00–10).
  Папка `docs/screenshots/section-1/` удалена.

Не проверено: другие браузеры, кроме Chromium; настоящие телефоны; программы
экранного доступа; диалог печати браузера (проверены эмуляция печати и PDF).

### PR 1, остальное: Сократ, словарная проверка, репозиторий (20 ноября 2026)

- Снятый текст: страница мыслителя и подборки без опубликованных текстов
  отвечает 404 (`apps/library/views.py`). Тесты `tests/test_withdrawal.py`:
  после `withdraw_publication` текста нет в библиотеке, поиске, на обложке и в
  блоке `Next question`; в тесте три текста (второй текст библиотеки проходит
  настоящий путь подачи, проверки с заглушкой и публикации), `Next question`
  текста о городе ведёт на опубликованный. Шаг снятия убран из серверного
  блока `README.md`; само снятие на сервере — в «Командах для сервера» PR.
- Словарная проверка (`apps/library/editorial.py`): считаются предложения
  частей и строки развилок (вопрос и три ответа; подписи кнопок — интерфейс).
  Доля A1 должна выполняться и в частях отдельно, и во всём тексте: иначе
  лёгкие строки развилок вытянули бы трудный текст, и проверка стала бы слабее
  прежней. Запись части измеряется целиком, строка развилки — по предложениям.
  Заглавная буква означает имя только внутри предложения; в начале предложения,
  после двоеточия и после открывающей кавычки слово ищется в списке.
  `vocabulary_profile` печатает итог и возвращает код 1 при отказе.
  Оба опубликованных текста проходят; их payload закреплён хешем в
  `tests/test_vocabulary_forks.py`. Заголовки частей и развилок по-прежнему не
  считаются: с ними текст о смелости не прошёл бы из-за слова `bigger`
  (упрощённый разбор форм не сводит его к `big`), а правка опубликованного
  текста требует новой платной проверки.
- Миграции: `CREATE EXTENSION IF NOT EXISTS vector` — первая операция
  `sources.0001_initial`. Проверено на базе из `template0`: до правки
  `type "vector" does not exist`, после — все миграции применяются. Применённые
  миграции Django повторно не выполняет, сервер не затронут. Тест
  `tests/test_migrations.py` следит за порядком.
- GitHub Actions (`.github/workflows/tests.yml`): pull request и push в `main`;
  чистый PostgreSQL 16 с pgvector, Python 3.12 (как на сервере), `check`,
  `makemigrations --check`, `migrate`, тесты.
- Удалены семь слитых веток (0 коммитов сверх `main` у каждой):
  `fork-per-part`, `section1-shots`, `section2-library-texts`,
  `section2-publication`, `kkuoii007-coder/shelf-stage`,
  `editorial-path-ask-switch`, `design/redesign-v1`. `redesign/frontend` не тронута.
- `README.md`, `docs/spec/CHANGELOG.md` (версия 1.5), `content-quality.md`:
  правило развилки, словарная проверка, команды `vocabulary_profile` и
  `import_editorial_source`.

После независимого ревью (20 ноября 2026):

- Слово, повторяющее первое слово записи с заглавной буквы, по-прежнему ищется
  в списке: без этого правило заглавной буквы пропускало текст, который прежняя
  проверка отклоняла по доле A1.
- Открывающими считаются также `„`, `‹` и прямая кавычка сразу после скобки,
  тире или запятой; началом предложения — и слово после знака многоточия.
- Строка развилки режется на предложения только там, где после `.`, `?`, `!`
  идёт заглавная буква: знак в середине предложения длину не обходит.
- Нет файла списка слов — подача отклоняется (раньше словарная проверка в этом
  случае молча пропускалась).
- Тест на страницу мыслителя и подборки, где один текст опубликован, а другой
  снят. В CI новый push отменяет прежний прогон только у pull request.

Оставлено как есть и названо в описании PR: словарная проверка стоит только на
подаче (`submit_text`), публикация её не повторяет; избранное на снятый текст
читатель убрать не может; `tests/test_fetcher.py` делает один DNS-запрос.

Не проверено: сервер (снятие текста о Сократе, состояние рабочего дерева,
применённые миграции) — команды в описании PR выполняет серверный агент.

## 20 ноября 2026. PR 1, читалка: после независимого ревью

Два дефекта поведения, найденные ревью, воспроизведены сценарием на прежнем
коде (32 невыполненных утверждения в новых шагах 12–15) и исправлены.

- Устаревшая страница затирала сохранённое место чтения. `choose()` и
  `openFromHash()` писали состояние из памяти страницы: вкладка, открытая
  раньше, или страница, возвращённая кнопкой «Назад», одним нажатием
  сбрасывала место, до которого читатель дошёл в другой вкладке. Теперь перед
  любой записью страница перечитывает сохранённое (`catchUp()`): `upto` не
  уменьшается, сохранённые ответы не теряются, ответ текущего нажатия
  побеждает только для своей развилки. На событие `storage` своего ключа и на
  `pageshow` с `persisted` страница перечитывает сохранённое и перерисовывается
  (`refresh()`), ничего не записывая и не двигая ни прокрутку, ни фокус.
  Ссылка `#part-N` записывает состояние, только если она что-то открыла.
- Поздно пришедший скрипт закрывал текст, который читатель уже видел. Запасной
  ход в `<head>` (снятие класса `js` через три секунды) теперь оставляет на
  `<html>` метку `data-js-off`; `init()` в `reader.js`, увидев её, не делает
  ничего. Страница остаётся обычной страницей без скрипта до следующей
  загрузки: весь текст, развилки без кнопок, русские строки видны. Переводы по
  нажатию и переключатель RU в этом случае не включаются: их включение прячет
  русские строки и подписи, которые читатель уже видит, и сдвигает текст.
- Исключение в `init()`: `catch` вызывает `plainPage()` — снимает
  `data-js-ready`, `js`, `ui-ru-off` и кнопочные атрибуты предложений. Раньше
  после сбоя на полпути на странице оставались кнопки развилок, подсказка и
  переключатель RU; теперь правило `html:not(.js)` в `main.css` прячет всё,
  что работает только со скриптом.

Проверки.

- `tools/qa/fork_steps.js`: упавший шаг даёт одну строку FAIL, прогон идёт
  дальше (раньше при убранном правиле цепочки сценарий падал с `SyntaxError`
  на разборе сохранённого состояния). Новые шаги: 12 — две вкладки (вторая
  следует за первой; вкладка, не получившая событие, отвечает и идёт по
  ссылке; сохранённое место позади страницы ничего не закрывает); 13 —
  `pageshow` (подложенное событие и настоящий возврат из кеша «Назад» в
  отдельном Chromium; если браузер страницу в кеше не держит, шаг помечается
  `skip`); 14 — `reader.js` задержан на 5 с; 15 — исключение в `init()` в
  начале и на последнем шаге. Полный прогон: 193 утверждения, все выполнены,
  три раза подряд, около 41 с.
- Тесты Django: 325 OK (1 ожидаемый провал). Добавлены проверки исходников в
  `tests/test_reading_path.py` (правила цепочки, запись только после
  `catchUp()`, цвет подсказки, `Words` и блок инструментов вне скрытия, поздний
  и сломанный скрипт) и `tests/test_browser_check_seed.py`.
- Пробные поломки (25, включая 11 из ревью): тесты Django ловят все 25.
  Сценарий ловит 23; две поломки в поведении не видны, потому что класс `js`
  всё равно снимает запасной ход в `<head>` («`catch` оставляет `js`»,
  «поздний скрипт включает переводы»), — их держат только проверки исходников.
- `tools/qa/seed_browser_check.py` готовит чистую базу для сценария: публикует
  оба текста из `content/texts/` редакционным путём (`submit_text` →
  `review_text --inline` → `publish_text`) с заглушкой проверяющего, создаёт
  читателя; `--olive-copy` и `--new-revision` — временная копия текста для
  тона olive и новой ревизии. Отказывается работать без `PR_BROWSER_CHECK=1`,
  при включённом `LIVE_AI_ENABLED`, при `AI_PROVIDER` не `fake` и при
  выключенном `DEBUG`. На сервере не запускать. Заглушка проверяющего теперь
  лежит в этом файле (в записи выше сказано, что в репозиторий она не входит).
- CI: задача `browser` в `.github/workflows/tests.yml` — чистая база, миграции,
  подготовка данных, `runserver`, Chromium для Playwright 1.59.0 (ставится в
  каталог шага, в репозиторий не входит), сценарий с тонами и новой ревизией.
  Задача `tests` не менялась. Три прогона задачи: зелёные, от 1 мин 41 с до
  2 мин 14 с. В CI настоящий возврат из кеша «Назад» пропускается: Chromium
  там страницу не удерживает и причину не называет (`masked`), итог — 189
  выполнено, 2 пропущено; подложенное событие `pageshow` проверяется всегда.

Не проверено: другие браузеры, кроме Chromium; настоящие телефоны; программы
экранного доступа; диалог печати браузера; сервер.
