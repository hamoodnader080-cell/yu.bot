@echo off
chcp 65001 > nul
echo ======================================================
echo    بوت مراقبة شواغر مواد جامعة اليرموك (YU Seat Bot)
echo ======================================================
echo.

if not exist .env (
    echo [!] ملف .env غير موجود. يتم إنشاء نسخة من .env.example...
    copy .env.example .env > nul
    echo [!] يرجى فتح ملف .env ووضع رمز توكن البوت BOT_TOKEN ثم إعادة التشغيل.
    notepad .env
    pause
    exit /b
)

echo [*] جاري تنظيف وإغلاق أي عمليات سابقة للبوت...
taskkill /F /IM python.exe 2>nul >nul

echo [*] جاري تشغيل البوت المحدث...
python bot.py

pause
