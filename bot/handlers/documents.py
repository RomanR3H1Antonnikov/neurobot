from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.keyboards.main_menu import BTN_DOCS, MENU_BUTTONS, main_menu_kb, inline_main_menu_kb
from bot.utils import cleanup_tracked_messages
from providers.base import ProviderError
from services import document_service
from services.media_service import InsufficientCreditsError, RateLimitError

router = Router()

SUPPORTED_EXTENSIONS = {"pdf", "docx", "doc", "xlsx", "xls", "txt", "csv"}

AWAITING_FILE_TEXT = (
    "📄 Пришли документ (PDF, Word, Excel, TXT) и я выполню любое задание по его содержимому.\n\n"
    "Поддерживаемые форматы: PDF, DOCX, XLSX, TXT, CSV"
)


class DocumentStates(StatesGroup):
    awaiting_file = State()
    awaiting_task = State()


def _file_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="docs:back:menu"))
    return builder.as_markup()


def _task_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="docs:back:file"))
    return builder.as_markup()


@router.message(F.text == BTN_DOCS)
async def enter_docs(message: Message, state: FSMContext) -> None:
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.clear()
    await state.set_state(DocumentStates.awaiting_file)
    await message.answer(AWAITING_FILE_TEXT, reply_markup=_file_kb())


@router.callback_query(F.data == "docs:back:menu")
async def docs_back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:docs")
async def menu_to_docs(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.delete()
    await state.clear()
    await state.set_state(DocumentStates.awaiting_file)
    await callback.message.answer(AWAITING_FILE_TEXT, reply_markup=_file_kb())


@router.callback_query(F.data == "docs:back:file")
async def docs_back_to_file(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DocumentStates.awaiting_file)
    await state.update_data(file_id=None, filename=None)
    await callback.message.edit_text(AWAITING_FILE_TEXT, reply_markup=_file_kb())
    await callback.answer()


@router.message(DocumentStates.awaiting_file, F.document)
async def receive_document(message: Message, state: FSMContext) -> None:
    doc = message.document
    filename = doc.file_name or "document"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext not in SUPPORTED_EXTENSIONS:
        await message.answer(
            f"❌ Формат «.{ext}» не поддерживается.\n"
            f"Пожалуйста, пришли: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
            reply_markup=_file_kb(),
        )
        return

    await state.update_data(file_id=doc.file_id, filename=filename)
    await state.set_state(DocumentStates.awaiting_task)
    await message.answer(
        f"✅ Файл «{filename}» получен.\n\n"
        "Теперь напиши задание — что нужно сделать с этим документом?\n"
        "<i>Например: «Сделай краткое резюме», «Переведи на английский», «Найди все даты»</i>",
        parse_mode="HTML",
        reply_markup=_task_kb(),
    )


@router.message(DocumentStates.awaiting_file, ~F.text.in_(MENU_BUTTONS))
async def docs_wrong_input(message: Message) -> None:
    await message.answer(
        "Пожалуйста, пришли документ (не фото и не текст).",
        reply_markup=_file_kb(),
    )


@router.message(DocumentStates.awaiting_task, F.text, ~F.text.in_(MENU_BUTTONS))
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


@router.message(DocumentStates.awaiting_task, ~F.text.in_(MENU_BUTTONS))
async def awaiting_task_wrong_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    filename = data.get("filename", "документ")
    await message.answer(
        f"Файл «{filename}» уже получен.\n\n"
        "Напиши текстом, что нужно сделать с этим документом:",
        reply_markup=_task_kb(),
    )


def _split_text(text: str, limit: int = 4096) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)]
