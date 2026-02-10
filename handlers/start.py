from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from config import MESSAGES

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        text=MESSAGES["welcome"],
        parse_mode="HTML"
    )
