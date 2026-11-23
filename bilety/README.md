# BiletY

Подготовка к экзамену по своим файлам. Загружаешь 1–5 файлов (PDF, Word, PowerPoint, фото конспекта, текст, ссылку), выбираешь функцию — ИИ (YandexGPT) делает:

1. **Все возможные билеты** по всем темам материала;
2. **Конспект**;
3. **Ответ на вопрос** — только по твоим файлам;
4. **Вопросы для самопроверки** карточками.

Работает с iPhone и iPad, ставится на экран «Домой» как приложение.

- Как устроено и что где менять: [docs/karkas.md](docs/karkas.md)
- Запуск в Yandex Cloud по шагам: [docs/yandex_cloud.md](docs/yandex_cloud.md)
- Проверка и скриншоты: [docs/proverka.md](docs/proverka.md)
- Философы на главной: [static/philosophers.json](static/philosophers.json)

Запуск на компьютере (пробный ИИ, без денег):
```
cd bilety
pip install -r requirements-dev.txt
BILETY_PASSWORD=test SESSION_SECRET=x KEY_SECRET=y AI_PROVIDER=mock uvicorn app.main:app --reload
python -m pytest -q tests
```
