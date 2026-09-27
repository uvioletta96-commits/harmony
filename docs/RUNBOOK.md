# Справка по операциям

Краткие рецепты для типовых задач. Полные объяснения — в [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Локальная разработка

```bash
make install                 # venv и зависимости
cp .env.example .env         # SECRET_KEY можно оставить пустым в dev
make migrate
make seed                    # демонстрационные данные
make dev                     # http://localhost:8000
```

```bash
make dev-celery              # фоновые задачи в отдельном терминале
```

Redis необязателен. Без него кэш и распределённый rate limit отключаются,
приложение пишет предупреждение в лог и продолжает работать.

---

## База данных

```bash
make migrate                 # применить миграции
make migrate-new m="add bookmarks"   # создать миграцию
make routes                  # все маршруты
```

```bash
# psql внутри контейнера
docker compose exec db psql -U harmony -d harmony

# статистика по строкам (для оценки роста)
docker compose exec cli flask shell -c \
  "from app.tasks.maintenance_tasks import db_stats; print(db_stats())"
```

---

## Пользователи

```bash
make create-admin            # интерактивно
make shell                   # произвольные команды в контексте приложения
```

```python
# во Flask shell
from app.models.user import User, UserRole, UserStatus
from app.extensions import db

u = db.session.query(User).filter_by(username="mira").one()
u.role = UserRole.MODERATOR.value
db.session.commit()
```

---

## Кэш

```bash
make flush-cache             # сбросить все ключи
```

```python
from app.services import cache_service
cache_service.invalidate_user("a1b2…", 42)      # профиль
cache_service.invalidate_feed()                    # вся лента
cache_service.delete_pattern("harmony:feed:*")     # по префиксу
```

---

## Фоновые задачи

```bash
# разово
docker compose run --rm cli shell -c \
  "from app.tasks.maintenance_tasks import purge_orphan_uploads; print(purge_orphan_uploads(dry_run=True))"

# очередь
docker compose exec worker celery -A celery_worker.celery_app inspect active
docker compose exec worker celery -A celery_worker.celery_app inspect registered | tr ',' '\n' | grep -E 'notifications|moderation|maintenance'
```

---

## Модерация

```bash
# что увидит локальный анализатор
docker compose exec backend flask shell -c "
from app.security.content_moderation import get_engine
for t in ['Обычный текст', 'Моя карта 4276 3800 1234 5678', 'Найду тебя и убью']:
    r = get_engine().screen(t, context='post')
    print(r.decision, r.score, r.categories)
"
```

Подробно — [MODERATION.md](MODERATION.md) и [MODERATION_POLICY.md](MODERATION_POLICY.md).

---

## Фронтенд

Файлы отдаются как есть, без сборки. Правка видна после обновления страницы.

```bash
make up                      # nginx на :80
docker compose logs -f web
```

```bash
# локально без Docker
cd frontend && python -m http.server 8000
```

```bash
# перегенерировать иконки после правки геометрии логотипа
python tools/generate_icons.py
```

---

## OpenAPI

```bash
make openapi                 # spec/openapi.yaml → backend/app/static/openapi.json
make openapi-check           # в CI валит сборку при расхождении
```

Интерактивная документация: `/api/docs`. Спецификация: `/api/openapi.json`.

---

## Наблюдаемость

```bash
make observability           # Grafana :3000, Prometheus :9090
make logs
docker compose ps
```

```bash
curl -s localhost:8000/healthz | jq
curl -s localhost:8000/readyz  | jq
curl -s localhost:8000/metrics | grep harmony_http_requests_total | head
```

Полезные метрики:

| Метрика | Что смотреть |
|---|---|
| `harmony_http_request_duration_seconds` | p95 латентности |
| `harmony_db_query_duration_seconds` | медленные запросы |
| `harmony_rate_limit_hits_total` | всплеск — атака или сломанный клиент |
| `harmony_celery_tasks_total{state="failure"}` | сломанные задачи |
| `harmony_moderation_decisions_total` | распределение решений |

---

## Резервные копии

```bash
make backup
ls -lh backups/

# целостность дампа
docker run --rm -v $PWD/backups:/b postgres:16-alpine \
  pg_restore -l /b/harmony-….dump > /dev/null && echo "ok"

# восстановление
make restore f=backups/harmony-20240101T000000Z.dump
```

Не забудьте файлы: `docker run --rm -v harmony_uploads:/u -v $PWD/backups:/b alpine \
  tar czf /b/uploads-….tar.gz -C /u .`

---

## Диагностика

| Симптом | Проверить |
|---|---|
| Бесконечный редирект | `TRUSTED_PROXY_COUNT` против числа прокси |
| 403 по CSRF | `harmony_csrf` в cookie, `SameSite` |
| Сокет не подключается | проброс `Upgrade` в nginx, `SOCKETIO_MESSAGE_QUEUE` |
| Письма не приходят | `MAIL_BACKEND=console`, таблица `email_deliveries` |
| Redis в логах | ожидаемая деградация; Redis поднимется через 30 с |
| API медленный | `harmony_db_query_duration_seconds`, затем `EXPLAIN ANALYZE` |
| 500 с трассировкой | `HARMONY_DEBUG_ERRORS=1 pytest …` |

---

## Git

```bash
git switch -c feat/quiet-mode
make check                    # lint + тесты
git commit -am "Добавить тихий режим ленты"
```

Перед коммитом убедитесь, что нет секретов:

```bash
git diff --cached | grep -iE 'secret|password|api_key' | grep -v 'process.env\|config\['
```
