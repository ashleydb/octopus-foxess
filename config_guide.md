### 📂 Configuration Guide (Finding Your Keys)
To get started, you need to fill in config.json. Here is exactly where to find each variable:
#### 1. Octopus Energy
 * **API Key (api_key):** Log in to your [Octopus Dashboard](https://octopus.energy/dashboard/new/accounts/personal-details/api-access?hl=en-GB), go to **Account Settings**, and scroll down to **Developer Settings**. Click "Generate" if you haven't already.
 * **Account Number (account):** This is visible at the top of your dashboard (starts with A-).
 * **MPAN (mpan):** In the **Developer Settings** area of your dashboard, look under "Electricity meter points". Your MPAN is the 13-digit number listed there.
#### 2. FoxESS
 * **API Key (api_key):** Log in to the [FoxESS Cloud](https://www.foxesscloud.com/bus/index?hl=en-GB) on a desktop browser. Hover over your user icon (avatar) in the top right, select **User Profile**, then **API Management**. Click **Generate API Key**.
   * *Note: If on mobile, you may need to request the "Desktop Site" in your mobile browser to see these menus.*
 * **Inverter SN (device_sn):** Found in the FoxESS app or web portal under **Devices**. It usually starts with a letter like 'E'.
#### 3. myenergi (Zappi)
 * **Hub Serial (hub_serial):** Found in your [myenergi account](https://myaccount.myenergi.com/?hl=en-GB) under **Manage Products**. It is a 7-digit number prefixed with SN.
 * **API Key (api_key):** In the same **Manage Products** section, click **Advanced** under your hub device and select **Generate new API Key**.
   * *Warning: Generating a new API key will replace your existing myenergi app password for this account.*
 * **Zappi Serial (zappi_serial):** Found in the myenergi app under **Menu > Settings > Device Settings** or on the physical label on the side of your Zappi unit.
#### 4. Telegram Notifications
 * **Bot Token (bot_token):** Search for @BotFather on Telegram. Message them /newbot and follow the prompts to name your bot. They will give you a long string of numbers and letters—that is your token.
 * **Chat ID (chat_id):**
   1.  Open your new bot on Telegram and press **Start**.
   2.  Send any message to the bot (e.g., "Hello").
   3.  In your web browser, visit: [https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates]()
   4.  Look for "chat":{"id":123456789}. That number is your chat_id.
### 🌐 Remote Access (Cloudflare Tunnel)
To view your dashboard safely on your phone without opening ports on your home router, use a Cloudflare Tunnel.
 1. **Create a Cloudflare Account:** It's free. Ensure your domain's DNS is managed by Cloudflare.
 2. **Install the Connector:** On your server (the one running the script), follow the [Cloudflare Tunnel installation guide](https://developers.cloudflare.com/workers-vpc/configuration/tunnel/#create-and-run-tunnel-cloudflared).
 3. **Configure the Tunnel:**
   * Go to **Zero Trust Dashboard > Networks > Tunnels**.
   * Create a new tunnel (e.g., "Home-Energy").
   * Add a **Public Hostname**:
     * **Subdomain:** energy (or whatever you like)
     * **Domain:** yourdomain.com
     * **Service Type:** HTTP
     * **URL:** localhost:8765
 4. **Secure with Cloudflare Access (Optional):** You can add an extra layer of security by going to **Access > Applications** and requiring an email PIN to even reach the login page.
### 🛠️ Configuration Checklist

| Category | Key | Typical Format |
| :--- | :--- | :--- |
| **Octopus** | api_key | sk_live_... |
| **Octopus** | account | A-1234ABCD |
| **FoxESS** | api_key | Long UUID string |
| **myenergi** | api_key | 16+ characters |
| **Telegram** | chat_id | 9-10 digit number |

Cloudflare Tunnel: Make Localhost Public Without Port Forwarding
This [video](https://youtu.be/etluT8UC-nw?si=H9JYCzaGMq73dzTS) provides a very clear, step-by-step walkthrough for setting up a permanent Cloudflare tunnel to expose local services like your dashboard to the internet securely.
