import pandas as pd
import base64
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import openai
from openai import OpenAI
import os
from docx import Document
from email.mime.application import MIMEApplication


# Constants
CSV_FILE_PATH = '/Users/akhil/Downloads/junk/Awards.csv' # csv file that contains the database of research projects : Their project's description, principal investigator's name and their email id, project's title
SCOPES = ['https://www.googleapis.com/auth/gmail.send'] # Don't change
CLIENT_SECRET_FILE = '/Users/akhil/Downloads/junk/client_secret_594050885167-uif92btfapv25ls7ujqicb12deas4vio.apps.googleusercontent.com.json' # Details mentioned in the README file
#'./client_secret_1023062743887-q485ao3kvmtgvia0gahrr5eq9oe60tar.apps.googleusercontent.com.json'
API_SERVICE_NAME = 'gmail'
API_VERSION = 'v1'
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
client = OpenAI(
    # This is the default and can be omitted
    api_key=OPENAI_API_KEY,
)
RESUME_DOC_PATH =  "/Users/akhil/Downloads/junk/AkhilRajResumeOriginal.docx" # Path where resume is in the .docx format, so that it can be provided in text format to open ai's api
RESUME_PDF_FILE_PATH = "/Users/akhil/Downloads/junk/AkhilRajResumeOriginal.pdf" # Path where resume is in pdf format, so that it can be attached to the mail(s)
SENDER_EMAIL_ID = 'ar2427@cornell.edu'
START_INDEX = 735 # Which row number of database to start from? This should by default be 0 unless we are continuing from somewhere in between

def get_gmail_service():
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES, redirect_uri='http://localhost:8080/') # make sure the port is not occupied!
    creds = flow.run_local_server(port=8080)
    service = build(API_SERVICE_NAME, API_VERSION, credentials=creds)
    return service

def create_message(sender, to, subject, message_text, resume_attachment):
    message = MIMEMultipart()
    message['to'] = to
    message['from'] = sender
    message['subject'] = subject
    msg = MIMEText(message_text, 'plain', 'utf-8')
    message.attach(msg)
    message.attach(resume_attachment)

    raw = base64.urlsafe_b64encode(message.as_bytes())
    return {'raw': raw.decode()}

def send_message(service, user_id, message):
    try:
        message = service.users().messages().send(userId=user_id, body=message).execute()
        print(f'Message Id: {message["id"]}')
    except HttpError as error:
        print(f'An error occurred: {error}')

def generate_email_content(project_title, pi_name, project_description, resume_string):

    query_for_subject = f"I am applying to request to be part of the following research project. Write a subject for a mail that would be an application to be part of the research project. Make sure the answer is within 1 line and MUST NOT seems to be written by an AI. \n\n\n project description : {project_description}"

    openai.api_key = OPENAI_API_KEY
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
        {
            "role": "user",
            "content":query_for_subject,
        }
        ],
    )
    subject = response.choices[0].message.content


    query_for_body = f"I am applying to do research work for the following research project. Write a cover letter for the following info in first person using the resume given. Make sure the answer is within 150 words and MUST NOT seems to be written by an AI. The points mentioned must be supported by my experiences with as much quantified metrics as possible. Keep experience at those institutes as preference in which work was most aligned to the info below. Make sure to include the experiences of NLP Research at Cornell Tech. Also add the line 'I am attaching my resume for your reference' at the end of the mail. Footer must be 'Thanks and Regards\nAkhil Raj\nCS 2024, Cornell University(Tech Campus)\nLinkedin : https://www.linkedin.com/in/akhil-raj-810261255/\nGithub(personal) : https://github.com/Akhil-Raj\nGithub(Cornell) : https://github.com/ar2427'\n\n\n Resume : \n {resume_string} \n\n\n Research Project Info : \n Principal Investigator : {pi_name} \n Project's description : {project_description}" # Change the footer in the string as desired

    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
        {
            "role": "user",
            "content": query_for_body,
        }
        ],
    )

    body = response.choices[0].message.content

    return subject, body

def read_resume(RESUME_DOC_PATH):
    resume_string = ""
    # Load the document
    doc = Document(RESUME_DOC_PATH)
    # Read and process each paragraph in the document
    for para in doc.paragraphs:
        resume_string += (para.text)
    return resume_string

def main():
    service = get_gmail_service()
    df = pd.read_csv(CSV_FILE_PATH, encoding="ISO-8859-1") # change encoding if required
    resume_string = read_resume(RESUME_DOC_PATH)

    # Check if file exists and attach it
    if os.path.isfile(RESUME_PDF_FILE_PATH):
        with open(RESUME_PDF_FILE_PATH, 'rb') as f:
            resume_attachment = MIMEApplication(f.read(), Name=os.path.basename(RESUME_PDF_FILE_PATH))
        resume_attachment['Content-Disposition'] = f'attachment; filename="{os.path.basename(RESUME_PDF_FILE_PATH)}"'
    else:
        print(f"File not found: {file_path}")

    count = 0
    
    for index, row in df.iloc[START_INDEX:].iterrows():
        count += 1
        project_title = row['Title'] # Change as per your needs
        pi_name = row['PrincipalInvestigator'] # change as per your needs
        project_description = row['Abstract'] # change as per your needs
        pi_email_id = row["PIEmailAddress"] # change as per your neeeds
        
        subject, body = generate_email_content(project_title, pi_name, project_description, resume_string)
        
        to = pi_email_id #"ar2427@cornell.edu" 
        # to = 'akhil.raj1997@gmail.com'
        message = create_message(SENDER_EMAIL_ID, to, subject, body, resume_attachment)
        print(f"Sending mail number {count} to {pi_name} at {pi_email_id}")
        send_message(service, 'me', message)

if __name__ == '__main__':
    main()

