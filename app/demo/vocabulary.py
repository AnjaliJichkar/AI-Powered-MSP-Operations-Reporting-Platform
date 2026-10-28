"""The public words the demo world is built from. Nothing here came from a tenant.

- **Company names** are the fictional companies Microsoft uses in its own
  documentation (Contoso, Fabrikam, Northwind and the rest).
- **People** are generated from short lists of common given names and surnames.
- **KB numbers** are Windows cumulative updates as Microsoft published them.
- **CVE ids** are public; the known-exploited ones are on CISA's KEV catalogue.
- **Software titles**, **hardware models** and **processors** are products as
  their vendors name them.
"""

from __future__ import annotations

FRANCHISEE_NAME = "Tailwind IT Services"
FRANCHISEE_SLUG = "tailwind-it-demo"

#: The franchisee's customers: (name, industry, size in devices). Eight customers
#: and the franchisee's own internal organisation, which the sync records inactive.
CUSTOMERS: tuple[tuple[str, str, int], ...] = (
    ("Contoso Ltd", "Legal Services", 46),
    ("Fabrikam Inc", "Manufacturing", 52),
    ("Northwind Traders", "Wholesale Distribution", 34),
    ("Adventure Works Cycles", "Retail", 41),
    ("Woodgrove Bank", "Financial Services", 28),
    ("Tailspin Toys", "Retail", 19),
    ("Litware Inc", "Software", 22),
    ("Fourth Coffee", "Hospitality", 12),
)
INTERNAL_ORGANISATION = FRANCHISEE_NAME  # named like the tenant: the sync records it internal

GIVEN_NAMES = (
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn",
    "Dana", "Robin", "Sam", "Jesse", "Drew", "Cameron", "Reese", "Skyler", "Harper",
    "Elliot", "Rowan", "Parker", "Sage", "Emerson", "Finley", "Blake", "Hayden",
)  # fmt: skip
SURNAMES = (
    "Adams", "Baker", "Carter", "Diaz", "Evans", "Foster", "Garcia", "Hughes", "Iyer",
    "Jensen", "Kim", "Lopez", "Murphy", "Nguyen", "Ortiz", "Patel", "Quinn", "Reed",
    "Singh", "Turner", "Underwood", "Vargas", "Walsh", "Xu", "Young", "Zimmerman",
)  # fmt: skip

#: Windows cumulative updates (KB, title, severity), as Microsoft published them.
X64 = " for x64-based Systems"
SERVER_21H2 = "Cumulative Update for Microsoft server operating system version 21H2"
OS_PATCHES: tuple[tuple[str, str, str], ...] = (
    ("KB5063878", "2025-08 Cumulative Update for Windows 11 Version 24H2" + X64, "CRITICAL"),
    ("KB5063709", "2025-08 Cumulative Update for Windows 10 Version 22H2" + X64, "CRITICAL"),
    ("KB5062553", "2025-07 Cumulative Update for Windows 11 Version 24H2" + X64, "CRITICAL"),
    ("KB5062554", "2025-07 Cumulative Update for Windows 10 Version 22H2" + X64, "CRITICAL"),
    ("KB5060842", "2025-06 Cumulative Update for Windows 11 Version 24H2" + X64, "IMPORTANT"),
    ("KB5060533", "2025-06 Cumulative Update for Windows 10 Version 22H2" + X64, "IMPORTANT"),
    ("KB5058411", "2025-05 Cumulative Update for Windows 11 Version 24H2" + X64, "IMPORTANT"),
    ("KB5058379", "2025-05 Cumulative Update for Windows 10 Version 22H2" + X64, "IMPORTANT"),
    ("KB5062572", "2025-07 " + SERVER_21H2 + X64, "CRITICAL"),
    ("KB5041160", "2024-08 " + SERVER_21H2 + X64, "CRITICAL"),
    ("KB5041022", "2024-08 Cumulative Update for .NET Framework 3.5 and 4.8.1 for Windows 11", "IMPORTANT"),  # noqa: E501
    ("KB2267602", "Security Intelligence Update for Microsoft Defender Antivirus", "NONE"),
    ("KB4052623", "Update for Microsoft Defender Antivirus antimalware platform", "OPTIONAL"),
)  # fmt: skip

#: Third-party products the patch engine updates: (product identifier, title, version).
SOFTWARE_PATCHES: tuple[tuple[str, str, str], ...] = (
    ("google-chrome", "Google Chrome (x64)", "139.0.7258.128"),
    ("microsoft-edge", "Microsoft Edge (x64)", "139.0.3405.111"),
    ("mozilla-firefox", "Mozilla Firefox (x64)", "142.0"),
    ("adobe-acrobat-reader-dc", "Adobe Acrobat Reader DC", "25.001.20630"),
    ("7-zip", "7-Zip (x64)", "25.01"),
    ("zoom", "Zoom Workplace", "6.5.7"),
    ("microsoft-teams", "Microsoft Teams (work or school)", "25.20.0"),
    ("notepad-plus-plus", "Notepad++ (x64)", "8.8.5"),
    ("vlc", "VLC media player", "3.0.21"),
    ("microsoft-365-apps", "Microsoft 365 Apps for enterprise (x64)", "16.0.19029.20136"),
)

#: Software an inventory lists, by (name, publisher, vulnerable version, fixed version).
SOFTWARE: tuple[tuple[str, str, str, str], ...] = (
    ("Google Chrome", "Google LLC", "127.0.6533.100", "139.0.7258.128"),
    ("Mozilla Firefox", "Mozilla", "128.0", "142.0"),
    ("Adobe Acrobat Reader DC", "Adobe", "24.002.20857", "25.001.20630"),
    ("7-Zip", "Igor Pavlov", "22.01", "25.01"),
    ("Zoom Workplace", "Zoom Video Communications, Inc.", "5.17.11", "6.5.7"),
    ("Notepad++", "Notepad++ Team", "8.5.8", "8.8.5"),
    ("VLC media player", "VideoLAN", "3.0.18", "3.0.21"),
    ("Java 8 Update 202", "Oracle Corporation", "8.0.2020.8", "8.0.4610.10"),
    ("WinRAR", "win.rar GmbH", "6.23", "7.12"),
    ("PuTTY", "Simon Tatham", "0.79", "0.83"),
    ("TeamViewer", "TeamViewer", "15.45.3", "15.68.5"),
    ("Dropbox", "Dropbox, Inc.", "180.4.5971", "222.4.5751"),
    ("Microsoft Teams classic", "Microsoft Corporation", "1.6.00.4472", "1.7.00.35953"),
    ("Cisco AnyConnect Secure Mobility Client", "Cisco Systems, Inc.", "4.10.07061", "5.1.9.113"),
)

#: Software that should not be on a managed computer: an `Uninstall` remediation.
SOFTWARE_TO_REMOVE: tuple[tuple[str, str], ...] = (
    ("uTorrent", "BitTorrent Inc."),
    ("CCleaner", "Piriform Software Ltd"),
    ("Java 6 Update 45", "Oracle"),
)

#: Public CVE ids. `kev` marks the ones on CISA's Known Exploited Vulnerabilities
#: catalogue; `product` is the software each is filed against here.
CVES: tuple[tuple[str, str, str, bool], ...] = (
    # (cve, product, severity, kev)
    ("CVE-2024-21412", "Windows SmartScreen", "CRITICAL", True),
    ("CVE-2024-30051", "Windows DWM Core Library", "CRITICAL", True),
    ("CVE-2024-38112", "Windows MSHTML Platform", "CRITICAL", True),
    ("CVE-2024-43451", "Windows NTLM", "HIGH", True),
    ("CVE-2024-49138", "Windows Common Log File System Driver", "CRITICAL", True),
    ("CVE-2023-23397", "Microsoft Outlook", "CRITICAL", True),
    ("CVE-2023-36884", "Microsoft Office", "CRITICAL", True),
    ("CVE-2023-38831", "WinRAR", "HIGH", True),
    ("CVE-2024-7971", "Google Chrome", "HIGH", True),
    ("CVE-2023-4863", "Google Chrome", "CRITICAL", True),
    ("CVE-2024-1709", "ConnectWise ScreenConnect", "CRITICAL", True),
    ("CVE-2021-44228", "Apache Log4j", "CRITICAL", True),
    ("CVE-2024-38063", "Windows TCP/IP", "CRITICAL", False),
    ("CVE-2024-30078", "Windows Wi-Fi Driver", "HIGH", False),
    ("CVE-2024-38077", "Windows Remote Desktop Licensing Service", "CRITICAL", False),
    ("CVE-2024-4761", "Google Chrome", "HIGH", False),
    ("CVE-2024-29943", "Mozilla Firefox", "CRITICAL", False),
    ("CVE-2024-41869", "Adobe Acrobat Reader DC", "HIGH", False),
    ("CVE-2024-11477", "7-Zip", "HIGH", False),
    ("CVE-2024-24919", "Zoom Workplace", "MEDIUM", False),
    ("CVE-2024-38428", "Notepad++", "MEDIUM", False),
    ("CVE-2024-46461", "VLC media player", "MEDIUM", False),
    ("CVE-2024-20918", "Java 8 Update 202", "HIGH", False),
    ("CVE-2024-31497", "PuTTY", "HIGH", False),
    ("CVE-2024-7479", "TeamViewer", "HIGH", False),
    ("CVE-2024-20337", "Cisco AnyConnect Secure Mobility Client", "MEDIUM", False),
    ("CVE-2024-38213", "Windows Mark of the Web", "MEDIUM", False),
    ("CVE-2024-38106", "Windows Kernel", "LOW", False),
)

#: Hardware: (manufacturer, model, processor, launched year, memory GB), workstations then servers.
WORKSTATION_MODELS: tuple[tuple[str, str, str, int], ...] = (
    ("Dell Inc.", "OptiPlex 7010", "12th Gen Intel(R) Core(TM) i5-12500", 16),
    ("Dell Inc.", "OptiPlex 7090", "11th Gen Intel(R) Core(TM) i7-11700", 16),
    ("Dell Inc.", "Latitude 5540", "13th Gen Intel(R) Core(TM) i5-1345U", 16),
    ("Dell Inc.", "Latitude 5420", "11th Gen Intel(R) Core(TM) i5-1145G7", 16),
    ("Dell Inc.", "OptiPlex 7040", "Intel(R) Core(TM) i5-6500 CPU @ 3.20GHz", 8),
    ("HP", "HP EliteDesk 800 G6", "10th Gen Intel(R) Core(TM) i5-10500", 16),
    ("HP", "HP ProBook 450 G8", "11th Gen Intel(R) Core(TM) i5-1135G7", 8),
    ("HP", "HP EliteBook 840 G10", "13th Gen Intel(R) Core(TM) i7-1355U", 32),
    ("LENOVO", "ThinkPad T14 Gen 4", "AMD Ryzen 7 PRO 7840U w/ Radeon 780M Graphics", 16),
    ("LENOVO", "ThinkCentre M70q Gen 3", "12th Gen Intel(R) Core(TM) i5-12400T", 16),
    ("LENOVO", "ThinkPad E15", "Intel(R) Core(TM) i5-8265U CPU @ 1.60GHz", 8),
    ("Microsoft Corporation", "Surface Laptop 5", "12th Gen Intel(R) Core(TM) i7-1265U", 16),
    ("Apple", "MacBookPro18,3", "Apple M1 Pro", 16),
    ("Apple", "Mac14,7", "Apple M2", 8),
)
SERVER_MODELS: tuple[tuple[str, str, str, int], ...] = (
    ("Dell Inc.", "PowerEdge R650", "Intel(R) Xeon(R) Silver 4314 CPU @ 2.40GHz", 128),
    ("Dell Inc.", "PowerEdge R740", "Intel(R) Xeon(R) Silver 4210 CPU @ 2.20GHz", 64),
    ("HPE", "ProLiant DL380 Gen10", "Intel(R) Xeon(R) Gold 5218 CPU @ 2.30GHz", 96),
    ("Dell Inc.", "PowerEdge T340", "Intel(R) Xeon(R) E-2246G CPU @ 3.60GHz", 32),
)
OS_WORKSTATION = (
    "Microsoft Windows 11 Pro Edition",
    "Microsoft Windows 11 Enterprise Edition",
    "Microsoft Windows 10 Pro Edition",
)
OS_MAC = ("macOS 14.6.1", "macOS 15.1")
OS_SERVER = (
    "Microsoft Windows Server 2022 Standard Edition",
    "Microsoft Windows Server 2019 Standard Edition",
)
OS_IOT = "Microsoft Windows 10 IoT Enterprise LTSC Edition"

#: Autotask ticket vocabulary: (title, issue type, sub-issue type).
TICKET_KINDS: tuple[tuple[str, str, str], ...] = (
    ("Cannot sign in to Outlook", "Email", "Outlook"),
    ("Password reset request", "User Access and Management", "Password Reset"),
    ("Printer offline in reception", "Printer/Scanner/Copier", "Printer"),
    ("Laptop running slowly", "Workstation (Laptop/Desktop)", "Performance"),
    ("Shared drive not mapping", "Shared Drive", "Permissions"),
    ("Teams audio not working", "Application/Software", "Microsoft Teams"),
    ("New starter setup", "User Access and Management", "Onboarding"),
    ("VPN disconnects every hour", "Network/Internet", "VPN"),
    ("Phishing email reported", "Cybersecurity", "Phishing"),
    ("QuickBooks will not open", "Application/Software", "Line of Business"),
    ("Second monitor not detected", "Workstation (Laptop/Desktop)", "Hardware"),
    ("Wi-Fi drops in the warehouse", "Network/Internet", "Wireless"),
    ("Leaver: disable account", "User Access and Management", "Offboarding"),
    ("Blue screen on startup", "Workstation (Laptop/Desktop)", "Crash"),
    ("Scanner to email fails", "Printer/Scanner/Copier", "Scan to Email"),
    ("Request: install Adobe Acrobat", "Application/Software", "Install Request"),
)
#: How each issue type was resolved: (root cause, what fixed it). The ticket
#: workspace cites resolved tickets for a similar one, so a resolution has to
#: say what was actually wrong and what was actually done, not "fixed".
RESOLUTIONS: dict[str, tuple[str, str]] = {
    "Cannot sign in to Outlook": (
        "the cached Exchange credential was stale after a password change",
        "cleared the Windows Credential Manager entries for MS.Outlook and "
        "office16, restarted Outlook and signed in again",
    ),
    "Password reset request": (
        "the account was locked out after repeated sign-ins from a phone with the old password",
        "reset the password, unlocked the account and removed the saved "
        "password from the phone's mail app",
    ),
    "Printer offline in reception": (
        "the print spooler had stopped and the printer's DHCP lease had moved it to a new address",
        "restarted the Print Spooler service, set a DHCP reservation for the "
        "printer and reconnected the queue by address",
    ),
    "Laptop running slowly": (
        "the disk was over 90% full and a third-party search indexer was running constantly",
        "cleared 40 GB of temporary and cached files, removed the indexer and "
        "confirmed disk latency back to normal",
    ),
    "Shared drive not mapping": (
        "the logon script ran before the VPN tunnel was up",
        "moved the drive map to a scheduled task triggered on network connect "
        "and re-mapped the drive by hand",
    ),
    "Teams audio not working": (
        "the headset was held open by another application and Teams had the "
        "wrong output device selected",
        "closed the application holding the device, set the headset as the "
        "default in Teams and tested a call",
    ),
    "New starter setup": (
        "new hire onboarding: no account, device or licence existed yet",
        "created the account, assigned the licence, enrolled the laptop and "
        "handed over with a first-day walkthrough",
    ),
    "VPN disconnects every hour": (
        "the VPN client's session lifetime was shorter than the idle timeout on the firewall",
        "raised the session lifetime on the client profile and confirmed a "
        "four-hour connection without a drop",
    ),
    "Phishing email reported": (
        "a credential-harvesting message passed the filter from a newly registered domain",
        "quarantined the message across all mailboxes, blocked the sender "
        "domain and confirmed no credentials were entered",
    ),
    "QuickBooks will not open": (
        "the company file was locked by a session left open on another machine",
        "closed the stale session, ran the file doctor and reopened the "
        "company file with the user watching",
    ),
    "Second monitor not detected": (
        "the dock's firmware was out of date and the cable had failed",
        "updated the dock firmware, replaced the DisplayPort cable and "
        "confirmed both monitors at full resolution",
    ),
    "Wi-Fi drops in the warehouse": (
        "two access points were on the same channel and one had lost power over Ethernet",
        "re-planned the channels, replaced the PoE injector and confirmed "
        "roaming across the warehouse",
    ),
    "Leaver: disable account": (
        "offboarding: the account and its sessions were still active after the leaving date",
        "disabled the account, revoked every session, converted the mailbox "
        "to shared and collected the device",
    ),
    "Blue screen on startup": (
        "a faulty storage driver shipped in a vendor update",
        "rolled the storage driver back to the previous version, confirmed "
        "three clean boots and blocked the update",
    ),
    "Scanner to email fails": (
        "the scanner still used the retired SMTP relay",
        "repointed the scanner at the current relay with the right "
        "authentication and sent a test scan",
    ),
    "Request: install Adobe Acrobat": (
        "software request: the user needed Acrobat for a client contract",
        "assigned the licence, deployed Acrobat through the RMM and confirmed "
        "activation with the user",
    ),
    "Disk space low on C: (below 10%)": (
        "Windows update servicing files and old user profiles filled the disk",
        "ran disk cleanup for servicing files, removed three stale profiles and left 96 GB free",
    ),
    "Device offline for more than 24 hours": (
        "the machine was powered off while the user was away",
        "confirmed with the user, powered it on and checked the agent had "
        "checked in and taken its updates",
    ),
    "Backup job failed": (
        "the backup ran while a database was mid-transaction and the snapshot was refused",
        "moved the job an hour later, re-ran it by hand and confirmed a clean restore point",
    ),
    "Windows service stopped: Print Spooler": (
        "a corrupt print job crashed the spooler",
        "cleared the spool directory, restarted the service and set it to "
        "restart automatically on failure",
    ),
    "Antivirus definitions out of date": (
        "the agent could not reach the update service through the new proxy",
        "added the update endpoints to the proxy allowlist and confirmed the definitions current",
    ),
}

#: Tickets a machine raises, not a person.
AUTOMATED_TICKETS: tuple[str, ...] = (
    "Disk space low on C: (below 10%)",
    "Device offline for more than 24 hours",
    "Backup job failed",
    "Windows service stopped: Print Spooler",
    "Antivirus definitions out of date",
)
AUTOMATION_CONTACT = "NinjaOne Monitoring"

#: Recent alert tickets, still open, in the shapes the alert-note workflows
#: answer: a title and the notice's body. One of each per customer, raised
#: in the last day and a half, so the demo tenant always has something for
#: each note workflow to read. The bodies are what the senders write — a
#: Veeam job report, an N-able status mail, a Blackpoint MDR alert — in their
#: own fixed layouts, which is what the backup parser is tested against.
RECENT_ALERTS: tuple[tuple[str, str], ...] = (
    (
        "Device offline for more than 24 hours",
        "{who} reports: device offline on {machine}. Please investigate.",
    ),
    (
        "Disk space low on C: (below 10%)",
        "{who} reports: disk space low on {machine}. Please investigate.",
    ),
    (
        "[Failed] Daily-{machine}",
        "Veeam Backup & Replication\nBackup job: Daily-{machine}\nStatus: Failed\n"
        "Error: Failed to connect to repository REPO01 (The network path was not found)\n",
    ),
    (
        "Backup completed on {machine}",
        "N-able Backup Manager\nDevice: {machine}\nBackup status: Completed\n",
    ),
    (
        "Malware detected on {machine}",
        "{who} reports: Bitdefender quarantined Trojan.GenericKD.71234567 on {machine}.",
    ),
    (
        "Blackpoint MDR alert: suspicious PowerShell on {machine}",
        "Blackpoint SOC: suspicious encoded PowerShell observed on {machine}. "
        "MDR analysts are reviewing.",
    ),
)

#: NinjaOne alert texts, by condition.
ALERTS: tuple[tuple[str, str], ...] = (
    ("Disk free space below 10%", "MAJOR"),
    ("Device offline for 24 hours", "MAJOR"),
    ("Memory usage above 95% for 15 minutes", "MINOR"),
    ("Windows service stopped: Print Spooler", "MODERATE"),
    ("Pending reboot for more than 7 days", "MINOR"),
    ("Patch installation failed", "MODERATE"),
    ("Antivirus definitions out of date", "MAJOR"),
    ("CPU usage above 90% for 30 minutes", "MINOR"),
)

#: GravityZone detection vocabulary: (threat name, action taken).
DETECTIONS: tuple[tuple[str, str], ...] = (
    ("Trojan.GenericKD.71234567", "Deleted"),
    ("Gen:Variant.Ursu.883912", "Quarantined"),
    ("Adware.Generic.1291013", "Deleted"),
    ("PUA.Bundler.Win32.Uniblue", "Quarantined"),
    ("Exploit.CVE-2023-23397.Gen", "Blocked"),
    ("Trojan.Downloader.Agent.XYZ", "Quarantined"),
)

STREETS = ("Main Street", "Commerce Way", "Industrial Park Road", "Market Square", "Harbour View")
CITIES = ("Austin", "Round Rock", "Cedar Park", "Georgetown", "Pflugerville", "San Marcos")
