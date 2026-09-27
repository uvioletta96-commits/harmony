# Интеграция внешнего модератора

Локальный анализатор (`MODERATION_PROVIDER=local`) работает всегда и
покрывает однозначные случаи: персональные данные, угрозы, язык ненависти,
инъекции, ссылки на мошенничество. Он не понимает контекста и не ловит
сарказм.

Внешний классификатор добавляет семантическое понимание. Он **необязателен** и
по определению может только **усилить** решение локального слоя — иначе его
сбой, исчерпание квоты или неверная настройка превратились бы в обход
модерации.

```
Локальный  block ───────────────────────────────▶ block
Локальный  review  +  внешний allow   ─────────▶ review
Локальный  allow   +  внешний review  ─────────▶ review
Локальный  allow   +  внешний block   ─────────▶ block
```

---

## Режим 1: OpenAI-совместимый API

Подходит для OpenAI и любого сервиса с тем же контрактом
(`/v1/moderations`).

```env
MODERATION_PROVIDER=openai
MODERATION_API_URL=https://api.openai.com/v1/moderations
MODERATION_API_KEY=sk-…
MODERATION_MODEL=omni-moderation-latest
MODERATION_TIMEOUT=4
```

**Запрос**

```http
POST /v1/moderations HTTP/1.1
Authorization: Bearer sk-…
Content-Type: application/json

{
  "model": "omni-moderation-latest",
  "input": "Текст публикации целиком"
}
```

**Ожидаемый ответ**

```json
{
  "results": [
    {
      "flagged": true,
      "categories": {
        "harassment/threatening": 0.91,
        "hate": 0.02,
        "self-harm": 0.0
      }
    }
  ]
}
```

Решение вычисляется по пиковой оценке категории:

| Пик | Решение |
|---|---|
| ≥ `MODERATION_BLOCK_THRESHOLD` (0.85) | `block` |
| ≥ `MODERATION_REVIEW_THRESHOLD` (0.55) или `flagged` | `review` |
| иначе | `allow` |

## Режим 2: произвольный HTTP-сервис

```env
MODERATION_PROVIDER=custom
MODERATION_API_URL=https://moderation.internal/v1/check
MODERATION_API_KEY=…
```

**Запрос**

```json
{ "text": "Текст публикации", "context": "post" }
```

`context` — `post`, `comment`, `message` или `update`.

**Ответ**

```json
{
  "decision": "review",
  "score": 0.62,
  "categories": ["insult"],
  "reasons": ["targeted language toward a named person"]
}
```

`decision` — `allow` | `review` | `block`. Любое нераспознанное значение
трактуется как `review`, а не как `allow`: неизвестный ответ сервиса не должен
превращаться в пропуск контента.

---

## Что происходит при сбое

Зависит от `MODERATION_FAIL_CLOSED`:

| Значение | Поведение | Когда применять |
|---|---|---|
| `false` (по умолчанию) | доверяем локальному слою, логируем `degraded` | сервис известен как надёжный, допустима кратковременная деградация |
| `true` | **всё** уходит на ручную проверку | цена ошибки модерации выше цены задержки |

Значение `false` выбрано по умолчанию, потому что `true` при недоступности
классификатора полностью останавливает публикацию контента — это заметная
деградация продукта. Включайте `true` для платформ, где пропуск вредного
контента недопустим.

## Рекомендации по эксплуатации

**Таймаут — 4 секунды.** Запрос идёт в пользовательский путь. Длинный таймаут
превращает задержку классификатора в задержку публикации.

**Повторных нет.** Модерация выполняется синхронно, один раз; при сбое
срабатывает политика `FAIL_CLOSED`. Сеть клиента не должна повторять запрос —
у публикации есть `idempotency`-семантика на уровне модели.

**Мониторьте `degraded`.** Событие `moderation.fail_mode` в логах означает, что
классификатор недоступен и решения принимает один локальный слой. Устойчивый
рост этого счётчика — повод разобраться, а не привыкнуть.

**Не передавайте лишнего.** Запрос содержит только текст. Ничего идентифицирующего
отправка не требует — это важно и для GDPR, и для договоров с провайдерами.

## Проверка интеграции

```python
from app import create_app
from app.security.content_moderation import get_engine, reset_engine

app = create_app()
with app.app_context():
    reset_engine()                      # перечитать конфигурацию
    for text in [
        "Спокойный разговор о книгах.",   # ожидается allow
        "Ссылка на мой профиль t.me/spam", # ожидается review или block
        "Нормальный текст, но с https://bit.ly/x https://bit.ly/y",
    ]:
        result = get_engine().screen(text, context="post")
        print(result.decision, result.score, result.categories)
```

## Обновление словаря

`MODERATION_DICTIONARY_PATH` — путь к файлу со словами, по одному в строке,
`#` для комментариев. Слова добавляются в набор ненормативной лексики
локального анализатора. Файл перечитывается при каждом создании движка, то
есть при перезапуске процесса.

```env
MODERATION_DICTIONARY_PATH=/app/dictionaries/ru.txt
```

```bash
# в контейнере
docker compose exec backend python - <<'PY'
from app.security.content_moderation import get_engine
engine = get_engine()
print("words loaded:", len(engine.local.profanity))
PY
```

## Проверка качества

`moderation.sweep_recent` раз в 4 часа перепроверяет опубликованное за 12
часов. Это даёт обратную связь: новое правило внешнего классификатора
начинает действовать на существующий контент без ручного прохода по таблице.
Результат — в `harmony_moderation_decisions_total`; изменения статуса
дополнительно логируются как `moderation.recheck_changed_status`.
