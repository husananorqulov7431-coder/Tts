# TTS — YouTube videolarini o‘zbekcha ovozlashtirish Telegram bot

Bot Telegram orqali avval YouTube havolasini, undan keyin mos `.srt` subtitr faylini oladi. Inline tugmalarda erkak/ayol ovozi tanlanadi va Google Translate orqali o‘zbekcha tarjima yoqiladi yoki o‘tkazib yuboriladi. GPT/Gemini kabi generativ AI modeli ishlatilmaydi. Edge TTS ovozni subtitr vaqtlariga moslaydi. Yakuniy videoda asl audio 10% darajada qoladi, video oqimi qayta kodlanmaydi.

## Muhim arxitektura

- Bot doimiy ishlashi uchun **Oracle Cloud VM** da Docker orqali ishlaydi.
- GitHub kodi va CI/CD uchun ishlatiladi. GitHub Actions’dagi bepul 2 000 daqiqa 24/7 bot uchun emas.
- Video oqimi qayta kodlanmaydi: `-c:v copy` orqali mavjud video oqimi saqlanadi. Faqat audio qayta aralashtiriladi.
- Telegram’ning lokal Bot API serveri katta fayllar bilan ishlash uchun qo‘shilgan. Bot API serveri odatda API orqali yuboriladigan fayl o‘lchami chegarasini oshirishga yordam beradi, lekin Telegram’ning amaldagi chegaralari va server resurslari baribir amal qiladi.
- Faqat `ALLOWED_USER_IDS` ro‘yxatidagi Telegram foydalanuvchilari botdan foydalana oladi.

## Telegram ma’lumotlari

1. Bot tokeni: [@BotFather](https://t.me/BotFather) → `/newbot`.
2. O‘z Telegram ID’ingiz: [@userinfobot](https://t.me/userinfobot).
3. Lokal Bot API uchun API ID va API Hash: [my.telegram.org/apps](https://my.telegram.org/apps).

**Tokenni kodga yozmang va ommaviy xabarda ulashmang.** GitHub Secrets orqali saqlang.

## GitHub Secrets

Repo → Settings → Secrets and variables → Actions sahifasida quyidagi `Secrets` yarating:

- `TELEGRAM_BOT_TOKEN` — BotFather bergan token.
- `ALLOWED_USER_IDS` — faqat sizning raqamli Telegram ID’ingiz; bir nechta bo‘lsa vergul bilan: `123456789,987654321`.
- `TELEGRAM_API_ID` — `my.telegram.org/apps` dan.
- `TELEGRAM_API_HASH` — `my.telegram.org/apps` dan.
- `ORACLE_HOST` — Oracle VM’ning public IPv4 manzili.
- `ORACLE_USER` — Ubuntu image uchun odatda `ubuntu`.
- `ORACLE_SSH_KEY` — VM’ga kiradigan SSH **private key**; public key esa serverdagi `~/.ssh/authorized_keys` faylida bo‘lishi kerak.
- `ORACLE_SSH_PORT` — odatda `22`.
- `ORACLE_KNOWN_HOSTS` — serverning tasdiqlangan SSH host-key satrlari; avval fingerprintni Oracle konsolidagi server kaliti bilan solishtirib, keyin `ssh-keyscan -p 22 PUBLIC_IP` natijasini saqlang.

Secrets sahifasi: https://github.com/husananorqulov7431-coder/Tts/settings/secrets/actions

`ORACLE_KNOWN_HOSTS` secret’iga oddiygina tekshirilmagan boshqa server kalitini ko‘chirib qo‘ymang: u deploy vaqtida SSH serverining kimligini tekshirish uchun ishlatiladi.

## GitHub Actions orqali 10 daqiqalik sinov (Oracle VM’siz)

Bu usul botni doimiy ishlatmaydi. Siz workflow’ni qo‘lda ishga tushirasiz; bot 10 daqiqagacha Telegram xabarlarini kutadi va bitta vazifa bajarilgach o‘zi to‘xtaydi. GitHub Actions vazifasining umumiy xavfsizlik limiti 12 daqiqa.

1. Repo → **Settings → Secrets and variables → Actions** sahifasida yuqoridagi `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` secret’larini kiriting.
2. **Actions** → **Run Telegram bot for up to 10 minutes** → **Run workflow** ni bosing.
3. Workflow ishga tushgach, Telegram’da botga `/start` yuboring. Avval YouTube linkini, keyin shu videoning `.srt` faylini yuboring.
4. Inline tugmalarda erkak/ayol ovozini va Google Translate sozlamasini tanlang. Tayyor video jarayon xabariga reply ko‘rinishida qaytadi.
5. Vazifa tugashi bilan bot va vaqtinchalik Docker ma’lumotlari tozalanadi. Hech qanday vazifa kelmasa, bot 10 daqiqa bo‘lgach o‘zi yopiladi.

**Muhim:** workflow 24/7 bot emas — uni har bir sinov oldidan qo‘lda ishga tushirish kerak. Katta video 10 daqiqalik oynaga sig‘masa, vaqt tugaganda jarayon bekor bo‘lishi mumkin. Doimiy bot uchun Oracle VM usuli kerak.

## Oracle VM’ni bir marta tayyorlash

Ubuntu VM yarating, SSH orqali kiring va Docker/Compose o‘rnating:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 rsync
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
sudo mkdir -p /opt/tts-bot
sudo chown "$USER":"$USER" /opt/tts-bot
```

Guruh o‘zgarishi kuchga kirishi uchun SSH’dan chiqib qayta kiring. Oracle Cloud Networking/Security List’da SSH porti 22 ochiq bo‘lsin. Bot API serveri Docker tarmog‘ida ishlaydi, 8081 portini internetga ochish shart emas.

Agar shu bot tokeni avval Telegram’ning odatiy cloud Bot API’sida polling uchun ishlatilgan bo‘lsa, lokal API serverga o‘tishdan oldin token uchun `logOut` bir marta bajarilishi kerak. Tokenni shell history’ga yozmaslik uchun terminalda tokenni yashirin kiritib, quyidagini bajaring:

```bash
read -s -p "Bot token: " BOT_TOKEN; echo
curl -sS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/logOut"
unset BOT_TOKEN
```

## Deploy

1. Yuqoridagi Secrets’ni kiriting va Oracle VM’ga SSH public key qo‘shilganini tekshiring.
2. GitHub → **Actions** → **Deploy to Oracle Cloud** → **Run workflow**.
3. Workflow kodni `/opt/tts-bot` ga nusxalaydi, maxfiy `.env` faylini yaratadi va `docker compose up -d --build` bajaradi.
4. Ishlash holati: VM’da `cd /opt/tts-bot && docker compose ps`; loglar: `docker compose logs -f bot`.

## Bot’dan foydalanish

1. Botga YouTube video havolasini yuboring.
2. O‘sha suhbatda mos `.srt` faylini yuboring.
3. Standart sozlamada SRT boshqa tilda bo‘lsa o‘zbekchaga tarjima qilinadi. Tarjima xizmati ishlamasa, bot xabar beradi va asl matnni ovozlashtirishga urinadi; bunday natija to‘liq o‘zbekcha bo‘lmasligi mumkin. SRT allaqachon o‘zbekcha bo‘lsa `/translate off` buyrug‘i bilan tarjimani o‘chiring.
4. Yakuniy video botga qaytadi. Standart ovoz — `uz-UZ-SardorNeural`; `/voice female` bilan Madina ovozini, `/voice male` bilan Sardor ovozini tanlang.
5. `/cancel` joriy topshiriqni kutish holatidan olib tashlaydi; allaqachon boshlangan FFmpeg jarayonini majburan to‘xtatmaydi.

## Sifat va cheklovlar

- Video oqimi mavjud eng yaxshi YouTube oqimidan tanlanadi va qayta kodlanmaydi. Bu video sifatini saqlaydi; audio AAC formatida qayta yaratiladi.
- Yakuniy video 2 GB dan katta bo‘lsa Telegram Bot API uni qabul qilmasligi mumkin.
- Tarjima va Edge TTS internetga bog‘liq; ularning limitlari yoki xizmatdagi o‘zgarishlar ishlashga ta’sir qilishi mumkin.
- Juda qisqa subtitr oynalarida gapni vaqtga moslash uchun ovoz tezlashtiriladi. Murakkab subtitrlar uchun sifat va sinxronlikni tekshirish tavsiya etiladi.
- YouTube kontentini faqat huquqingiz bor yoki qayta ishlashga ruxsatingiz mavjud videolarda ishlating.
