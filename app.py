import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.dispatcher.router import Router
from aiogram.fsm.context import FSMContext
from aiogram.filters.state import State, StatesGroup
import whisper
import openai
import re
import googlemaps
import aiohttp
import csv
import io
import smtplib
import aiofiles
from datetime import datetime
from email.message import EmailMessage


class EmailStates(StatesGroup):
    awaiting_sender_email = State()
    awaiting_password = State()
    awaiting_email_theme = State()
    awaiting_draft_review = State()
    awaiting_csv_source = State()  # Состояние для выбора источника CSV
    awaiting_csv_upload = State()


# Define your states
awaiting_email = True


GOOGLE_MAPS_API_KEY = 'Api-key'

# Initialize Google Maps client
gmaps = googlemaps.Client(key=GOOGLE_MAPS_API_KEY)

# Set up logging to display information in the console.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Bot token obtained from BotFather in Telegram.
TOKEN = 'api-key'
bot = Bot(token=TOKEN)
router = Router()
router_email = Router()
router_search = Router()

# Load Whisper model
model = whisper.load_model("tiny")

# Set your OpenAI API key here
openai.api_key = 'api-key'

# Ключ API и ID поисковой системы для Google Custom Search
GOOGLE_API_KEY = 'AIzaSyCmmg1m0kOMaoRN_k-CHE3_Anf5RbcMOTc'
GOOGLE_CX = 'd040208d062344b7e'


# Define a message handler for the "/start" command.
@router.message(Command("start"))
async def start_message(message: types.Message):
    await message.answer("Hello! Use /search your query by text and after search use /send_email to start sending ")


async def handle_voice(message: types.Message):
    file_info = await bot.get_file(message.voice.file_id)
    file_path = await bot.download_file(file_info.file_path)
    with open("voice_message.ogg", "wb") as f:
        f.write(file_path.read())
    result = model.transcribe("voice_message.ogg")
    text = result['text']
    logger.info(f"Transcribed text from voice: {text}")
    await handle_text_query(message, text)


@router_search.message(Command("search"))
async def handle_text_query(message: types.Message, state: FSMContext):
    user_input = message.text
    queries = await generate_search_queries(user_input)
    all_results = []
    for query in queries:
        # Очистка запроса: удаление номеров запросов и кавычек
        clean_query = re.sub(r'^\d+\.\s*"', '', query).strip('"')
        if clean_query:  # Ensure query is not empty
            results = await google_search_and_extract(clean_query)
            all_results.extend(results)

    if not all_results:
        await message.answer("No results found.")
        return

    # Combine all results and send them
    response_text = "Here are some companies found:\n\n"
    for name, website, emails in all_results:
        email_list = ", ".join(emails)
        response_text += f"**{name}**:\nWebsite: {website}\nEmails: {email_list}\n\n"
    await send_csv(message.chat.id, all_results)


async def generate_search_queries(user_input):
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "Generate three diverse search queries for local business information based on the user's input."},
                {"role": "user", "content": user_input}
            ],
            max_tokens=150
        )
        if 'choices' in response and response['choices']:
            full_text = response['choices'][0]['message']['content'].strip()
            queries = full_text.split("\n")  # Splitting by newline to separate the queries
            queries = [query.strip().strip('"') for query in queries if query]  # Clean up each query
            if len(queries) < 3:
                queries += [""] * (3 - len(queries))  # Ensure there are exactly three queries
            logger.info(f"Generated Queries: {queries}")
            return queries
        else:
            logger.warning("No choices returned by GPT-3.")
            return [""] * 3
    except Exception as e:
        logger.error(f"Error generating GPT queries: {str(e)}")
        return [""] * 3


async def google_search_and_extract(query):
    search_result = await fetch_places(query)
    info = await process_search_results(search_result)
    # Пока существует токен следующей страницы, продолжаем делать запросы
    while 'next_page_token' in search_result:
        await asyncio.sleep(5)  # Google требует задержку перед использованием токена следующей страницы
        search_result = await fetch_places(query, search_result['next_page_token'])

        # Обработка полученных результатов
        more_info = await process_search_results(search_result)
        info.extend(more_info)

    return info


async def fetch_places(query, page_token=None):
    """Отправляет запрос к Google Places API и возвращает результаты."""
    try:
        if page_token:
            # Запрос следующей страницы результатов
            return gmaps.places(query=query, page_token=page_token)
        else:
            # Первоначальный запрос
            return gmaps.places(query=query)
    except Exception as e:
        logger.error(f"Error during fetching places: {str(e)}")
        return {}  # Возвращаем пустой словарь в случае ошибки


async def process_search_results(search_result):
    info = []
    if search_result['status'] == 'OK':
        async with aiohttp.ClientSession() as session:
            tasks = []
            for place in search_result['results']:
                place_id = place['place_id']
                place_details = gmaps.place(place_id=place_id, fields=['name', 'website'])
                company_name = place_details['result'].get('name')
                website = place_details['result'].get('website', 'No website found')
                if website != 'No website found':
                    task = (company_name, website, fetch_and_parse_website(session, website))
                    tasks.append(task)

            results = await asyncio.gather(*[t[2] for t in tasks])
            for (company_name, website, _), emails in zip(tasks, results):
                if emails:  # Добавляем сайты, где найдены email
                    info.append((company_name, website, emails))

    return info


async def fetch_and_parse_website(session, url):
    try:
        async with session.get(url) as response:
            html_content = await response.text()
            emails = parse_html(html_content)
            return emails
    except Exception as e:
        logger.error(f"Error fetching or parsing {url}: {str(e)}")
        return []


def parse_html(html_content):
    emails = set(re.findall(r"\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]{2,}\b", html_content))
    return filter_emails(emails)


def filter_emails(emails):
    ignore_patterns = [
        r'sentry\..+',
        r'wixpress\.com',
        r'polyfill\.io',
        r'lodash\.com',
        r'core-js-bundle\.com',
        r'react-dom\.com',
        r'react\.com',
        r'npm\.js',
        r'@[a-zA-Z0-9]*[0-9]{5,}@',
        r'\b[a-zA-Z]+@[0-9]+\.[0-9]+\.[0-9]+\b',  # Игнорирование email с числовыми значениями (напр., версии)
        r'@\w*\.png',  # Игнорирование ссылок на PNG файлы
        r'@\w*\.jpg',  # Игнорирование ссылок на JPG файлы
        r'@\w*\.jpeg',  # Игнорирование ссылок на JPEG файлы
        r'@\w*\.gif',  # Игнорирование ссылок на GIF файлы
        r'\w+-v\d+@3x-\d+x\d+\.png',
        r'\w+-v\d+@3x-\d+x\d+\.png.webp',
        r'[a-zA-Z0-9_\-]+@[0-9]+x[0-9]+\.png',
        r'[a-zA-Z0-9_\-]+@[0-9]+x[0-9]+\.jpeg',
        r'[a-zA-Z0-9_\-]+@[0-9]+x[0-9]+\.png.webp',
        r'[a-zA-Z0-9_\-]+@[\d]+x[\d]+\.png',
        r'[a-zA-Z0-9_\-]+@\d+x\d+\.(png|jpg|jpeg|gif)',
        r'[a-zA-Z0-9_\-]+-v\d+_?\d*@[0-9]+x[0-9]+\.png',
        r'[a-zA-Z0-9_\-]+-v\d+_?\d*@[0-9]+x[0-9]+\.png.webp',
        r'IASC',
        r'@\w*\.png.webp',
        r'Mesa-de-trabajo'
    ]
    return [email for email in emails if not any(re.search(pattern, email) for pattern in ignore_patterns)]


async def send_csv(chat_id, data):
    # Создаем CSV файл в памяти
    output = io.StringIO()
    writer = csv.writer(output)
    # Записываем заголовки
    writer.writerow(['Company Name', 'Website', 'Emails'])
    # Записываем данные
    for name, website, emails in data:
        writer.writerow([name, website, ', '.join(emails)])
    # Перемещаем указатель в начало файла
    output.seek(0)
    # Создаем объект FormData
    form_data = aiohttp.FormData()
    form_data.add_field('document', output, filename='companies.csv')
    # Отправляем CSV файл
    async with aiohttp.ClientSession() as session:
        async with session.post(f'https://api.telegram.org/bot{TOKEN}/sendDocument?chat_id={chat_id}',
                                data=form_data) as resp:
            if resp.status != 200:
                print(await resp.text())


# Define your command handler to start the process
# Start the command to input the sender's email address
@router.message(Command("send_email"))
async def send_email_command(message: types.Message, state: FSMContext):
    await message.answer("Please enter your email address:")
    await state.set_state(EmailStates.awaiting_sender_email)


# Utility function to validate an email address format
def is_valid_email(email):
    pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    return re.match(pattern, email) is not None


# Handle the email address input from the user
@router.message(EmailStates.awaiting_sender_email)
async def handle_sender_email(message: types.Message, state: FSMContext):
    sender_email = message.text
    if is_valid_email(sender_email):
        await message.answer("Sender email set. Please enter your password for SMTP authentication:")
        await state.update_data(sender_email=sender_email)
        await state.set_state(EmailStates.awaiting_password)
    else:
        await message.answer("Please enter a valid email address.")


# Handle the password input for SMTP authentication
@router.message(EmailStates.awaiting_password)
async def handle_password(message: types.Message, state: FSMContext):
    password = message.text
    await state.update_data(password=password)
    await message.answer("Password set. What is the theme or main content for your email?")
    await state.set_state(EmailStates.awaiting_email_theme)


# Handle the email theme/content input and generate a draft using OpenAI
@router.message(EmailStates.awaiting_email_theme)
async def handle_email_theme(message: types.Message, state: FSMContext):
    prompt = message.text
    draft = await generate_email_content(prompt)
    if draft:
        await message.answer("Here is a draft based on your input:\n{}\nDo you approve this draft? Type 'yes' to approve, or provide your corrections.".format(draft))
        await state.update_data(draft=draft)
        await state.set_state(EmailStates.awaiting_draft_review)
    else:
        await message.answer("Failed to generate draft, please try entering the theme again.")


# Generate email content using OpenAI
async def generate_email_content(prompt):
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4-turbo",
            messages=[{"role": "system", "content": "You are a skilled email writer. Create a professional email draft based on the user's provided theme. Use max 100 tokens"}, {"role": "user", "content": prompt}],
            max_tokens=100
        )
        content = response.choices[0].message.content.strip()
        return content
    except Exception as e:
        print(f"Error generating email content: {str(e)}")
        return None


# Handle the review and approval of the generated email draft
@router.message(EmailStates.awaiting_draft_review)
async def handle_draft_review(message: types.Message, state: FSMContext):
    if message.text:
        response = message.text.lower()
        if response == 'yes':
            await message.answer("Please type 'upload' to upload your CSV or 'default' to use the default CSV.")
            await state.set_state(EmailStates.awaiting_csv_source)
        else:
            await state.update_data(draft=response)
            await message.answer("Draft updated. Type 'yes' to send or provide further corrections.")
    else:
        await message.answer("Please send a text response.")


# Handle the selection between uploading a new CSV file or using a default CSV file
@router.message(EmailStates.awaiting_csv_source)
async def choose_csv_source(message: types.Message, state: FSMContext):
    if message.text:
        user_input = message.text.lower()
        if user_input == 'upload':
            await message.answer("Please upload your CSV file.")
            await state.set_state(EmailStates.awaiting_csv_upload)
        elif user_input == 'default':
            data = await state.get_data()
            sender_email = data['sender_email']
            draft = data['draft']
            await send_emails_from_csv(sender_email, 'YOUR_EMAIL_PASSWORD', 'Subject of your emails', draft, "default.csv")
            await message.answer("Emails have been sent successfully using the default CSV.")
            await state.clear()
        else:
            await message.answer("Please type 'upload' to upload your CSV or 'default' to use the default CSV.")
    else:
        await message.answer("Please send a text message indicating your choice.")


# Updated handler to upload a CSV file and send emails
@router.message(EmailStates.awaiting_csv_upload)
async def handle_document(message: types.Message, state: FSMContext):
    if message.document:
        document_id = message.document.file_id
        file_info = await bot.get_file(document_id)
        file_path = await bot.download_file(file_info.file_path)

        unique_filename = f"user_uploaded_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"

        async with aiofiles.open(unique_filename, "wb") as f:
            await f.write(file_path.read())
            await f.close()

        data = await state.get_data()
        sender_email = data['sender_email']
        sender_password = data['password']
        draft = data['draft']  # Clear the draft before use
        await send_emails_from_csv(sender_email, sender_password, 'Subject of your emails', draft, unique_filename)
        await message.answer(f"Emails have been sent successfully using your uploaded CSV: {unique_filename}.")
        await state.clear()
    else:
        await message.answer("Please upload a CSV file.")


# Function to send an email via SMTP
def send_email(sender_email, sender_password, recipient_email, subject, content):
    print("Preparing message content...")

    # Create the email message object
    msg = EmailMessage()

    # Set the content with UTF-8 charset
    msg.set_content(content, charset='utf-8')

    # Set other headers
    msg['From'] = sender_email
    msg['To'] = recipient_email
    msg['Subject'] = subject

    smtp_server = "smtp.gmail.com"
    smtp_port = 587

    try:
        print("Connecting to SMTP server...")
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            print("Logging in...")
            server.login(sender_email, sender_password)
            print("Sending email...")
            server.send_message(msg)
            print(f"Email successfully sent to {recipient_email} using {smtp_server}!")
            return True
    except Exception as e:
        print(f"Failed to send email via {smtp_server}: {str(e)}")
        return False


# Updated function to read email addresses from a CSV file and send emails asynchronously
async def send_emails_from_csv(sender_email, sender_password, subject, content, csv_filename):
    """Asynchronously read email addresses from a CSV file and send emails via Gmail SMTP."""
    success_count = 0
    fail_count = 0

    try:
        async with aiofiles.open(csv_filename, mode='r', encoding='utf-8') as csvfile:
            contents = await csvfile.read()
            reader = csv.reader(contents.splitlines(), delimiter=';')
            next(reader)  # Skip the header

            for row in reader:
                print(f"Processing row: {row}")
                if len(row) >= 3:
                    recipient_email = row[2]  # Extract the email from the third column
                    success = send_email(sender_email, sender_password, recipient_email, subject, content)
                    if success:
                        success_count += 1
                        print(f"Email successfully sent to {recipient_email}")
                    else:
                        fail_count += 1
                else:
                    print('Incomplete row found, skipping...')

    except Exception as e:
        print(f"Error reading file or processing data: {str(e)}")

    print(f"Total emails processed: {success_count + fail_count}, Sent: {success_count}, Failed: {fail_count}")


# Main function to start the bot.
async def main():
    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(router_email)
    dp.include_router(router_search)
    await dp.start_polling(bot)

if __name__ == '__main__':
    # Initialize the email list
    email_list = []
    asyncio.run(main())
