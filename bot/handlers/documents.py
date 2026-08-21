from aiogram import Router, F
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.keyboards.main_menu import BTN_DOCS, main_menu_kb
from providers.base import ProviderError
from services import document_service
from services.media_service import InsufficientCreditsError, RateLimitError

router = Router()

SUPPORTED_EXTENSIONS = {"pdf", "docx", "doc", "xlsx", "xls", "txt", "csv"}


class DocumentStates(StatesGroup):
    awaiting_file = State()
    awaiting_task = State()


@router.message(F.text == BTN_DOCS)
async def enter_docs(message: Message, state: FSMContext) -> None:
    await state.set_state(DocumentStates.awaiting_file)
    await message.answer(
        "📄 Пришли документ (PDF, Word, Excel, TXT) и я выполню любое задание по его содержимому.\n\n"
        "Поддерживаемые форматы: PDF, DOCX, XLSX, TXT, CSV"
    )


@router.message(DocumentStates.awaiting_file, F.document)
async def receive_document(message: Message, state: FSMContext) -> None:
    doc = message.document
    filename = doc.file_name or "document"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext not in SUPPORTED_EXTENSIONS:
        await message.answer(
            f"❌ Формат «.{ext}» не поддерживается.\n"
            f"Пожалуйста, пришли: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
        return

    await state.update_data(file_id=doc.file_id, filename=filename)
    await state.set_state(DocumentStates.awaiting_task)
    await message.answer(
        f"✅ Файл «{filename}» получен.\n\n"
        "Теперь напиши задание — что нужно сделать с этим документом?\n"
        "<i>Например: «Сделай краткое резюме», «Переведи на английский», «Найди все даты»</i>",
        parse_mode="HTML",
    )


@router.message(DocumentStates.awaiting_file)
async def docs_wrong_input(message: Message) -> None:
    await message.answer("Пожалуйста, пришли документ (не фото и не текст).")


@router.message(DocumentStates.awaiting_task, F.text)
async def receive_task(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_text = message.text

    processing_msg = await message.answer("📖 Читаю документ и выполняю задание...")

    try:
        file_info = await message.bot.get_file(data["file_id"])
        file_bytes_io = await message.bot.download_file(file_info.file_path)
        file_bytes = file_bytes_io.read()

        result = await document_service.process_document(
            message.from_user.id,
            message.from_user.username,
            data["filename"],
            file_bytes,
            task_text,
        )

        await processing_msg.delete()
        # длинный ответ разбиваем на части по 4096 символов
        for chunk in _split_text(result):
            await message.answer(chunk)

        await state.clear()
        await message.answer("Готово! Что-то ещё?", reply_markup=main_menu_kb())

    except InsufficientCreditsError as e:
        await processing_msg.delete()
        await message.answer(f"❌ {e}\n\nПополни баланс в разделе «Мой баланс».")
    except RateLimitError as e:
        await processing_msg.delete()
        await message.answer(f"⏱ {e}")
    except ProviderError:
        await processing_msg.delete()
        await message.answer("⚠️ Сервис временно недоступен. Попробуй позже.")
    except Exception:
        await processing_msg.delete()
        await message.answer("⚠️ Не удалось обработать документ. Попробуй позже.")


def _split_text(text: str, limit: int = 4096) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)]
