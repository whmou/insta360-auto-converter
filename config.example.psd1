@{
    # Keep this tracked example fake. Store real values outside the repository,
    # preferably in %LOCALAPPDATA%\Insta360AutoConverter\config.psd1.
    DataDirectory     = "C:\path\to\insta360-auto-converter-data"
    SdkExecutable     = "C:\path\to\Insta360-SDK\bin\MediaSDKTest.exe"

    # Generated locally after Google sign-in. It grants access to the authorized
    # YouTube account and is refreshed during normal runs. This is generally the
    # more sensitive OAuth file. Never commit or share it.
    TokenFile         = "C:\path\to\insta360-auto-converter-data\youtube_auth.json"

    # Downloaded from Google Cloud for an OAuth Desktop app. It identifies the app
    # and is needed for initial authorization or re-authorization, not every run.
    # It is still a secret: never commit or share it.
    ClientSecretFile  = "C:\path\to\client_secret.json"
    OutputDirectory   = "C:\path\to\insta360-auto-converter-data\sd-output"
    ExpectedChannelId = "YOUR_CHANNEL_ID"
    LoginHint         = ""
    DefaultPlaylistId = ""
    DailyLimit        = 80
    Privacy           = "unlisted"
}
