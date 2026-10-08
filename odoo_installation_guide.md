# Complete Beginner's Guide to Odoo & Hikvision Integration

Since you are completely new to Odoo, don't worry! This guide will walk you through exactly what Odoo is, where to get it, how to install it on your Windows computer, and how to load the code we just wrote.

## 1. What is Odoo?
Odoo is a popular business management software (like an ERP). It runs in your web browser, but it requires a server running in the background on your computer to work. 
The code you have in your folder (`attendance_punching_integration`) is known as an **Odoo Addon (or Module)**. It adds specific features (like reading from your Hikvision device) to the base Odoo system.

---

## 2. Download the Latest Odoo (Version 18)
Odoo provides an easy-to-use installer for Windows that bundles everything you need (Python, PostgreSQL database, and the Odoo server).

1. Go to the official Odoo download page: [https://www.odoo.com/page/download](https://www.odoo.com/page/download)
2. Fill in your basic details (Name, Company, Email) to unlock the download buttons.
3. Under **Odoo 18** (the latest version), click the **Download** button for the **Windows (Community Edition)**. 
   *(Note: Community is free, Enterprise is paid. Community is perfect for what you are doing).*

---

## 3. Install Odoo on Windows
1. Once the `.exe` file downloads, double-click it to start the installation.
2. Select your language and click **Next**.
3. Agree to the terms.
4. On the "Select Components" screen, **make sure both "Odoo Server" and "PostgreSQL Database" are checked**. Click **Next**.
5. Keep the default Hostname, Port, Username, and Password for PostgreSQL. *(Important: Take a screenshot or write down the PostgreSQL Username and Password just in case, though Odoo usually handles this automatically).*
6. Choose an installation folder (e.g., `C:\Program Files\Odoo 18.0`) and click **Install**. 
7. This will take a few minutes. Once done, it will automatically open your web browser to `http://localhost:8069`.

---

## 4. Initial Database Setup
When your browser opens `http://localhost:8069`:
1. You will see a screen asking to create a database.
2. **Master Password**: This is provided at the top of the screen. Keep it safe!
3. **Database Name**: Give it a name (e.g., `my_company`).
4. **Email**: Enter your email (this will be your admin login username).
5. **Password**: Create a password (this will be your admin login password).
6. **Demo Data**: Leave this unchecked.
7. Click **Create Database**. 

You are now inside Odoo!

---

## 5. Install Python Dependencies
The custom code we wrote needs two Python libraries (`requests` for Hikvision and `zk` for ZKTeco) to communicate with the devices. We must install them into Odoo's Python environment.

1. Open your Windows **Start Menu**.
2. Type `cmd` to find the **Command Prompt**, right-click it, and select **Run as administrator**.
3. In the command prompt, navigate to the Odoo python folder. Assuming you installed it in the default location, type:
   ```cmd
   cd "C:\Program Files\Odoo 18.0\python"
   ```
4. Now, install the required libraries by typing:
   ```cmd
   python -m pip install requests zk
   ```
5. Wait for it to say "Successfully installed...".

---

## 6. Copy Your Custom Module into Odoo
We need to put your `attendance_punching_integration` folder into the place where Odoo looks for custom apps.

1. Open your File Explorer.
2. Copy your folder: `d:\Research\ASHIQ\Odoo\attendance_punching_integration`
3. Navigate to your Odoo Addons folder. By default, this is:
   `C:\Program Files\Odoo 18.0\server\odoo\addons\`
4. Paste the `attendance_punching_integration` folder directly inside the `addons` folder.

---

## 7. Restart Odoo
Because you added a new folder and installed Python packages, Odoo needs a quick restart to notice them.

1. Open your Windows **Start Menu**.
2. Type `Services` and open the **Services** app.
3. Scroll down the list until you find a service named something like **odoo-server-18.0**.
4. Right-click it and click **Restart**.

---

## 8. Install Your App in Odoo
Now we activate your custom code inside the Odoo web interface.

1. Go back to your web browser (`http://localhost:8069`).
2. Go to the **Settings** app (if you don't see Settings, go to the top-left menu icon to see all apps).
3. Scroll to the very bottom of the Settings page and click **Activate the developer mode**. (A little bug icon will appear at the top of your screen).
4. Go to the **Apps** menu (top-left icon -> Apps).
5. On the top menu bar, click **Update Apps List** and then click **Update** in the popup.
6. Now, go to the search bar on the Apps page. Remove the "Apps" filter, and search for `attendance_punching_integration`.
7. You should see your module appear! Click the **Activate** button on it.

---

## 9. Configure Your Hikvision Device
Now that the app is installed:

1. Click the top-left menu icon and you will see a new app called **Attendances** (or **Attendance Punching** depending on the menu layout).
2. Go to **Attendance Punching** (or Settings -> Attendance Punching).
3. Click **New** to create a device.
4. **Name**: e.g., "Front Door Scanner"
5. **Device Type**: Select **Hikvision**.
6. **Device IP**: Enter the IP address of your Hikvision device (e.g., `192.168.1.64`).
7. **Port No**: Usually `80`.
8. **Username**: Usually `admin`.
9. **Password**: The password you use to log into the Hikvision device's web interface.
10. Click the save icon (the cloud icon at the top).

## 10. Fetch Attendance
To test it, simply click the **Fetch Attendance** button at the top of the device configuration page. It will reach out to the Hikvision device and pull in all the latest check-in logs!
