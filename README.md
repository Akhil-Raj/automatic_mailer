# automatic_mailer
automatically mails with LLM generated content

STEPS :

- Get OPENAI_API_KEY(https://help.openai.com/en/articles/4936850-where-do-i-find-my-openai-api-key)
- Get google's CLIENT_SECRET_FILE

    - To obtain the CLIENT_SECRET_FILE for using Google APIs such as Gmail, you'll need to create a project in the Google Cloud Console, enable the Gmail API, and then create credentials to access the API. Here’s a step-by-step guide to help you through the process:

        - Step 1: Create a Google Cloud Project
Go to the Google Cloud Console.
If you haven’t already, sign in with your Google account.
Click on "Select a project" at the top of the dashboard, then click on “New Project”.
Enter a project name and select a billing account if applicable. Click "Create".
        - Step 2: Enable the Gmail API
Once your project is created and selected, navigate to the "APIs & Services" dashboard.
Click on “+ ENABLE APIS AND SERVICES”.
Search for "Gmail API" and select it from the list.
Click "Enable" to activate the Gmail API for your project.
        - Step 3: Create Credentials
With the Gmail API enabled, go to the “Credentials” tab on the left-hand side menu.
Click on “+ CREATE CREDENTIALS” at the top of the page.
Select “OAuth client ID”.
You may be prompted to configure the "OAuth consent screen" first. If so:
Click on the “Configure consent screen” button.
Select the “External” user type for testing, or “Internal” if your app will only be used within your organization.
Fill out the required fields on the consent screen setup form, such as the app name, user support email, and developer contact information. Save and continue.
You don’t need to add scopes manually on the Scopes page; just save and continue.
Add any test users if necessary (for external applications), then save and continue.
After setting up the consent screen, return to the “Create OAuth client ID” page:
Application type: Choose “Web application”.
Name your OAuth 2.0 client.
Under “Authorized redirect URIs”, add the URI provided by your application or the tool you are using that will authenticate using this client ID. For local development, you might use something like http://localhost:8080.
Click “Create”. Your client credentials will be displayed.
        - Step 4: Download the JSON File
After creating your OAuth client ID, you will see a page that shows your client ID and client secret.
There will be a download icon (looks like a download arrow) on the right side of your client ID. Click this icon to download the JSON file.
This downloaded JSON file is your CLIENT_SECRET_FILE. You should reference this file in your code to authenticate API requests.

Final Note
Keep the JSON file secure: This file contains sensitive information that allows access to your Google account via the API.
Set the correct scopes: When setting up the OAuth consent screen, ensure you choose the appropriate scopes for the actions your application needs to perform.
If you need more specific help during any of these steps or integrating this into your application, feel free to ask!


