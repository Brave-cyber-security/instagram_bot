from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from config import MESSAGES

router = Router(name="help")

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        text=MESSAGES["help"],
        parse_mode="HTML"
    )
