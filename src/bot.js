const { Telegraf } = require('telegraf');
const dotenv = require('dotenv');

dotenv.config();

const token = process.env.BOT_TOKEN;
if (!token) {
  console.error('BOT_TOKEN не найден. Добавьте его в .env или переменные окружения.');
  process.exit(1);
}

const bot = new Telegraf(token);

bot.start((ctx) => {
  ctx.reply(
    '👋 Привет! Я базовый бот FinUsluga. Отправь /help, чтобы узнать команды.'
  );
});

bot.help((ctx) => {
  ctx.reply('Доступные команды:\n/start — приветствие\n/help — список команд\n/ping — проверить связь');
});

bot.command('ping', (ctx) => ctx.reply('pong 🏓'));

bot.on('text', (ctx) => {
  ctx.reply(
    `Я пока echo-бот. Ты написал: "${ctx.message.text}". Спроси /help для доступных команд.`
  );
});

bot.launch().then(() => {
  console.log('FinUslugaBot запущен');
});

process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));
