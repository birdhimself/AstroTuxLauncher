#!/usr/bin/python3

import os
from os import path
import argparse
import json
import tomli, tomli_w
import dataclasses
from dataclasses import dataclass, field
from dataclasses_json import dataclass_json, config
from typing import Optional, List
from utils.misc import ExcludeIfNone, read_build_version, LAUNCHER_VERSION, CONTROL_CODES_SUPPORTED
from utils.termutils import set_window_title
from enum import Enum
from pansi import ansi
import utils.interface as interface
import logging
import sys
from queue import Queue
import shutil
from utils import steam
from utils.net import get_request
from packaging import version
import astro.playfab as playfab
from astro.dedicatedserver import AstroDedicatedServer, ServerStatus
import utils.net as net
import signal
import subprocess
import time
import traceback


"""
Code based on https://github.com/ricky-davis/AstroLauncher
"""

LOGGER = logging.getLogger("Launcher")

BANNER_LOGO = f"""{ansi.weight.bold}
    {ansi.BLUE}___         __           {ansi.YELLOW}______          
   {ansi.BLUE}/   |  _____/ /__________{ansi.YELLOW}/_  __/_  ___  __
  {ansi.BLUE}/ /| | / ___/ __/ ___/ __ \\{ansi.YELLOW}/ / / / / / |/_/
 {ansi.BLUE}/ ___ |(__  ) /_/ /  / /_/ {ansi.YELLOW}/ / / /_/ />  <  
{ansi.BLUE}/_/  |_/____/\\__/_/   \\____{ansi.YELLOW}/_/  \\__,_/_/|_|  {ansi.reset}
"""
BANNER_SUBTITLE = "L a u n c h e r".center(45)

BANNER_TEXT="Unofficial Astroneer Dedicated Server Launcher for Linux"


#
#   Constants
#

NAME = "AstroTuxLauncher"

HELP_COMMAND = f"""What {NAME} should do

    - install: Installs the Astroneer Dedicated Server using steamcmd
    - start: Starts the installed dedicated server
    - update: Updates the Astroneer Dedicated Server using steamcmd
"""

DEPOTDL_PATH = "libs/depotdownloader"
DS_EXECUTABLE = "AstroServer.exe"

ASTRO_SERVER_STATS_URL = "https://astroservercheck.joejoetv.de/api/stats"

class LauncherCommand(Enum):
    """ Represents the command passed to the launcher """
    
    START = "start"
    INSTALL = "install"
    UPDATE = "update"
    GENCONFIG = "genconfig"



#
#   Configuration classes
#

class NotificationMethod(Enum):
    """ Represents, which notification method should be used """
    
    NONE = ""
    NTFY = "ntfy"
    DISCORD = "discord"

@dataclass
class DiscordConfig:
    webhookURL: str = None

@dataclass
class NTFYConfig:
    topic: str = None
    serverURL: str = "https://ntfy.sh"

@dataclass
class NotificationConfig:
    method: NotificationMethod = NotificationMethod.NONE
    name: str = "Astroneer Dedicated Server"
    EventWhitelist: List[interface.EventType] = field(default_factory=lambda: [e for e in interface.EventType])
    
    discord: Optional[DiscordConfig] = field(metadata=config(exclude=ExcludeIfNone), default=None)
    ntfy: Optional[NTFYConfig] = field(metadata=config(exclude=ExcludeIfNone), default=None)

@dataclass
class StatusConfig:
    SendStatus: bool = False    # Wether to send status updates
    Interval: int = 120         # Interval in which to send status updates
    EndpointURL: str = ""       # URL to send status updates as GET requests to

@dataclass_json
@dataclass
class LauncherConfig:
    AutoUpdateServer: bool = True   # Wether to automatically install/update the Astroneer DS at start if update is available
    
    CheckNetwork: bool = True       # Wether to perform a network check before starting the Astroneer DS
    OverwritePublicIP: bool = False # Wether to overwrite the PublicIP DS config option with the fetched public IP
    
    # Settings related to notifications
    notifications: NotificationConfig = field(default_factory=NotificationConfig)    # Configuration for notifications
    
    # Settings related to sending status updates
    status: StatusConfig = field(default_factory=StatusConfig)
    
    LogDebugMessages: bool = False  # Wether the the console and log file should include log messages with level logging.DEBUG
    
    AstroServerPath: str = "AstroneerServer"    # The path, where the Astroneer DS installation should reside
    OverrideWinePath: Optional[str] = field(metadata=config(exclude=ExcludeIfNone), default=None)   # Path to wine executable, only used, if set
    WinePrefixPath: str = "winepfx"             # The path, where the Wine prefix should be stored
    WineBootTimeout: int = 30                   # The time (in seconds) that Wine will wait when running *Wineboot* before it times out
    LogPath: str = "logs"                       # The path where logs should be saved
    
    PlayfabAPIInterval: int = 2                 # Time to wait between Playfab API requests
    ServerStatusInterval: float = 3             # Time to wait between Server Status checks
    
    DisableEncryption: bool = False  # Wether to disable encryption for the Astroneer DS. CURRENTLY REQUIRED TO BE "True" FOR HOSTING ON LINUX

    WrapperPath: Optional[str] = field(metadata=config(exclude=ExcludeIfNone), default=None) # Optional wrapper to run Wine with (e.g. box64)

    @staticmethod
    def ensure_toml_config(config_path):
        """
            Reads the launcher configuration and fist creates the config file if not present, populated with the default values
        """
        
        config = None
        
        if path.exists(config_path):
            # If config file exists, read it into a config object
            if not path.isfile(config_path):
                raise ValueError("Specified config path doesn't point to a file!")
            
            with open(config_path, "rb") as tf:
                toml_dict = tomli.load(tf)
            
            # If no "launcher" section is present in the file, create it as empty
            if not ("launcher" in toml_dict.keys()):
                toml_dict = {"launcher": {}}
            
            config = LauncherConfig.from_dict(toml_dict["launcher"])

        else:
            # If config file is not present, create directories and default config
            if not path.exists(path.dirname(config_path)):
                os.makedirs(path.dirname(config_path))
            
            config = LauncherConfig()
        
        # Write config back to file to add missing entried and remove superflous ones
        # In the case of the file not existing prior, it will be created
        config_dict = {"launcher": config.to_dict(encode_json=True)}
        
        with open(config_path, "wb") as tf:
            tomli_w.dump(config_dict, tf)
        
        return config

class AstroTuxLauncher():
    
    def __init__(self, config_path, astro_path, depotdl_exec, force_debug_log=False):
        self.dedicatedserver = None
        self.status_thread = None
        
        # Setup basic logging
        interface.LauncherLogging.prepare()
        interface.LauncherLogging.setup_console()
        
        try:
            self.config_path = path.abspath(config_path)
            
            LOGGER.info(f"Configuration file path: {self.config_path}")
            
            self.config = LauncherConfig.ensure_toml_config(self.config_path)
        except Exception as e:
            LOGGER.error(f"Error while loading config file ({type(e).__name__}): {str(e)}")
            LOGGER.error(f"Please check the config path parameter and/or config file")
            self.exit()
        
        # If cli parameter is specified, it overrides the config value
        if not (astro_path is None):
            self.config = dataclasses.replace(self.config, AstroServerPath=astro_path)
        
        # If flag was passed, overrule config option
        if force_debug_log:
            self.config.LogDebugMessages = True
        
        # Make sure we use absolute paths
        self.config.AstroServerPath = path.abspath(self.config.AstroServerPath)
        self.config.WinePrefixPath = path.abspath(self.config.WinePrefixPath)
        self.config.LogPath = path.abspath(self.config.LogPath)
        
        # Apply wine path override if possible and check that is exists
        self.wineexec = shutil.which("wine")
        self.wineserverexec = shutil.which("wineserver")
        
        if self.config.OverrideWinePath is not None and path.isfile(self.config.OverrideWinePath):
            self.wineexec = path.abspath(self.config.OverrideWinePath)
            self.wineserverexec = path.join(path.dirname(self.wineexec), "wineserver")
        
        if (self.wineexec is None) or (self.wineserverexec is None):
            LOGGER.error("Wine (or Wineserver) executable not found!")
            LOGGER.error("Make sure that you have wine installed and accessible")
            LOGGER.error("or set 'OverrideWinePath' config option to the path of the wine executable")
            self.exit()
        
        # Finish setting up logging
        interface.LauncherLogging.set_log_debug(self.config.LogDebugMessages)
        interface.LauncherLogging.setup_logfile(self.config.LogPath)
        
        self.launcherPath = os.getcwd()
        
        self.depotdl_path = None
        
        # If argument is given, file has to exist
        if depotdl_exec:
            # If {depotdl_exec} is a command, get full path
            wpath = shutil.which(depotdl_exec)
            if wpath is not None:
                depotdl_exec = wpath
            
            if path.isfile(depotdl_exec):
                self.depotdl_path = path.abspath(depotdl_exec)
                LOGGER.info(f"DepotDownloader path overridden: {self.depotdl_path}")
            else:
                LOGGER.warning("The given DepotDownloader path doesn't point to a file, using default path")
        
        # If argument is not given, default path is used and may not exists yet, so create directories
        if self.depotdl_path is None:
            self.depotdl_path = path.abspath(DEPOTDL_PATH)
            os.makedirs(path.dirname(self.depotdl_path), exist_ok=True)
        
        # Log some information about loaded paths, configs, etc.
        LOGGER.info(f"Working directory: {self.launcherPath}")
        LOGGER.debug(f"Launcher configuration (including overrides):\n{json.dumps(self.config.to_dict(encode_json=True), indent=4)}")
        
        # Initialize console command parser
        self.console_parser = interface.ConsoleParser()
        self.cmd_queue = Queue()
        
        # Initialize Input Thread to handle console input later. Don't start thread just yet
        self.input_thread = interface.KeyboardThread(self.on_input, True)
        
        # Initialize thread for sending status updates to endpoint
        self.status_thread = interface.StatusUpdaterThread(self.config.status.EndpointURL, timeout=self.config.status.Interval, status=False)
        
        # Initialize notification objects
        self.notifications = interface.NotificationManager()
        
        self.notifications.add_handler(interface.LoggingNotificationHandler())
        
        if self.config.notifications.method == NotificationMethod.DISCORD:
            if self.config.notifications.discord.webhookURL:
                self.notifications.add_handler(interface.DiscordNotificationHandler(self.config.notifications.discord.webhookURL, name=self.config.notifications.name, event_whitelist=set(self.config.notifications.EventWhitelist)))
            else:
                LOGGER.warning("Discord Webhook URL is not set in config, not sending Discord notifications")
        elif self.config.notifications.method == NotificationMethod.NTFY:
            if self.config.notifications.ntfy.topic:
                self.notifications.add_handler(interface.NTFYNotificationHandler(self.config.notifications.ntfy.topic, ntfy_url=self.config.notifications.ntfy.serverURL, name=self.config.notifications.name, event_whitelist=set(self.config.notifications.EventWhitelist)))
            else:
                LOGGER.warning("ntfy topic is not set in config, not sending ntfy notifications")
        
        # Create Dedicated Server object
        self.dedicatedserver = AstroDedicatedServer(self)
    
    def check_ds_executable(self):
        """ Checks is Astroneer DS executable exists and is a file """
        
        execpath = os.path.join(self.config.AstroServerPath, DS_EXECUTABLE)
        
        return os.path.exists(execpath) and os.path.isfile(execpath)

    def on_input(self, input_string):
        """ Callback method to handle console input """
        
        # Parse console input
        success, result = self.console_parser.parse_input(input_string)
        
        if success:
            if result["cmd"] == interface.ConsoleParser.Command.HELP:
                # If it's a help command, we don't need to add it to the command queue as there is nothing to be done
                LOGGER.info(result["message"])
            else:
                # Add any other command to the command queue to be processed later
                self.cmd_queue.put(result)
        else:
            # If an error occured, {result} is just a message, so log it to console
            # We send event for command first, when it's processed
            LOGGER.warning(result)

    def update_wine_prefix(self):
        """
            Creates/updated the WINE prefix
        """
        
        LOGGER.debug("Ensuring WINE prefix is setup...")
        timeout = self.config.WineBootTimeout
        cmd = [self.wineexec, "wineboot"]

        if self.config.WrapperPath:
            cmd.insert(0, self.config.WrapperPath)

        env = os.environ.copy()
        
        # Remove DISPLAY environment variable to stop wine from creating a window
        if "DISPLAY" in env:
            del env["DISPLAY"]
        
        env["WINEPREFIX"] = self.config.WinePrefixPath
        env["WINEDEBUG"] = "-all"
        
        LOGGER.debug(f"Executing command '{' '.join(cmd)}' in WINE prefix '{self.config.WinePrefixPath}'...")
        
        try:
            wineprocess = subprocess.Popen(
                cmd,
                env=env,
                cwd=self.config.AstroServerPath,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                close_fds=True
            )
            code = wineprocess.wait(timeout=timeout)  # Use timeout defined from the config value
        except subprocess.TimeoutExpired:
            LOGGER.debug(f"Wine process took longer than {timeout} seconds, aborting")
            return False
        except Exception as e:
            LOGGER.error(f"Error occurred during updating of wine prefix: {str(e)}")
            return False
        
        return code == 0
    
    def check_network_config(self):
        if not self.dedicatedserver:
            raise ValueError("Dedcated Server has to be created first")
        
        LOGGER.info("Checking Network Configuration...")
        
        # Check if server port is reachable from local network over UDP
        server_local_reachable = net.net_test_local(self.dedicatedserver.ds_config.PublicIP, self.dedicatedserver.engine_config.Port, False)
        
        # Check if server post is reachable from internet over UDP
        server_nonlocal_reachable = net.net_test_nonlocal(self.dedicatedserver.ds_config.PublicIP, self.dedicatedserver.engine_config.Port)
        
        test_res = (server_local_reachable, server_nonlocal_reachable)
        
        if test_res == (True, True):
            LOGGER.info("Network configuration looks good")
        elif test_res == (False, True):
            LOGGER.warning("The Server is not accessible from the local network")
            LOGGER.warning("This usually indicates an issue with NAT Loopback")
        elif test_res == (True, False):
            LOGGER.warning("The server can be reached locally, but not from outside of the local network")
            LOGGER.warning(f"Make sure the Server Port ({self.dedicatedserver.engine_config.Port}) is forwarded for UDP traffic")
        elif test_res == (False, False):
            LOGGER.warning("The Server is completely unreachable")
            LOGGER.warning(f"Make sure the Server Port ({self.dedicatedserver.engine_config.Port}) is forwarded for UDP traffic and check firewall settings")
        
        rcon_local_blocked = not net.net_test_local(self.dedicatedserver.ds_config.PublicIP, self.dedicatedserver.ds_config.ConsolePort, True)
        
        if rcon_local_blocked:
            LOGGER.info("RCON network configuration looks good")
        else:
            LOGGER.warning(f"SECURITY ALERT: The RCON Port ({self.dedicatedserver.ds_config.ConsolePort}) is accessible from outside")
            LOGGER.warning("SECURITY ALERT: This potentially allows access to the Remote Console from outside your network")
            LOGGER.warning("SECURITY ALERT: Disable this ASAP to prevent issues")
            
            # kept from AstroLauncher
            time.sleep(5)
    
    def update_server(self):
        """
            Installs/Updates the Astroneer Dedicated Server.
            Also ensures that DepotDownloader is present
        """
        
        # If DepotDownloader executable doesn't exists yet, download it
        if not path.exists(self.depotdl_path):
            LOGGER.info("DepotDownloader not found, downloading...")
            steam.dl_depotdownloader(path.dirname(self.depotdl_path), path.basename(self.depotdl_path))
        
        LOGGER.info("Updating Astroneer Dedicated Server app from Steam...")
        success = steam.update_app(exec_path=self.depotdl_path, app="728470", os="windows", directory=self.config.AstroServerPath)
        
        self.buildversion = read_build_version(self.config.AstroServerPath)
        
        if success and (self.buildversion is not None):
            LOGGER.info(f"Sucessfully updated Astroneer Dedicated Server to version {self.buildversion}")
        else:
            LOGGER.error("Error while updating Astroneer Dedicated Server")
    
    def check_server_update(self, force_update=False):
        """
            Checks if an update for the Astroneer Dedicated Server is available or if it needs to be installed.
            Also performs update if set in config or {force_update} is set to True
        """
        
        oldversion = read_build_version(self.config.AstroServerPath)
        
        do_update = False
        installed = True
        
        if (oldversion is None) or not self.check_ds_executable():
            # No version is present yet or executable not present, we need an update/installation
            LOGGER.warning("Astroneer Dedicated Server is not installed yet")
            do_update = True
            installed = False
        else:
            # Get current server version from Spycibot endpoint
            try:
                data = json.load(get_request(ASTRO_SERVER_STATS_URL))
                newversion = data["stats"]["latestVersion"]
                
                if version.parse(newversion) > version.parse(oldversion):
                    LOGGER.warning(f"Astroneer Dedicated Server update available ({oldversion} -> {newversion})")
                    do_update = True
            except Exception as e:
                LOGGER.error(f"Error occured while checking for newest version: {str(e)}")
                LOGGER.warning(f"Trying server update blindly...")
                do_update = True

        if do_update:
            if self.config.AutoUpdateServer:
                if installed:
                    LOGGER.info("Automatically updating Astroneer Dedicated Server...")
                else:
                    LOGGER.info("Automatically installing Astroneer Dedicated Server...")
            
            if self.config.AutoUpdateServer or force_update:
                self.update_server()
            else:
                LOGGER.info("Not installing/updating automatically")
        else:
            if force_update:
                LOGGER.info("Noting to do")
            else:
                LOGGER.info("No update available, the Astroneer Dedicated Server is on the newest version")
        
    def start_server(self):
        """
            Starts the Astroneer Dedicated Server after setting up environment
        """
        
        # Check for and install DS update if wanted
        self.check_server_update()
        
        # If Playfab API can't be reached, we can't continue
        if not playfab.check_api_health():
            LOGGER.error("Playfab API is unavailable. Are you connected to the internet?")
            self.exit(reason="Playfab API unavailable")
        
        # Make sure wine prefix is ready
        if not self.update_wine_prefix():
            self.exit(reason="Error while updating WINE prefix")
        
        # Check that ports are available for the Server and RCON
        if not self.dedicatedserver.check_ports_free():
            self.exit(reason="Port not available")
        
        # Check netowrk configuration
        if self.config.CheckNetwork:
            self.check_network_config()
        
        LOGGER.debug("Starting input thread...")
        self.input_thread.start()
        
        # Prepare and start dedicated server
        try:
            if not self.dedicatedserver.start():
                return
        except Exception as e:
            LOGGER.error(f"There as an error while starting the Dedicated Server: {str(e)}")
            self.exit(reason="Error while starting Dedicated Server")
        
        LOGGER.info("Enter 'help' to get help about command usage")
        
        self.status_thread.update_status(status=True, message="Server is running")
        
        # If sending of status updates is enabled, start thread
        if self.config.status.SendStatus:
            LOGGER.info("Sending of status updates is enabled")
            self.status_thread.start()
        
        # Run Server Loop
        LOGGER.debug("Starting server loop...")
        self.dedicatedserver.server_loop()
    
    
    def user_exit(self, signal, frame):
        """ Callback for when user requests to exit the application """
        self.exit(graceful=True, reason="Received SIGINT signal")
    
    def exit(self, graceful=False, reason=None):
        if graceful:
            if reason:
                LOGGER.info(f"Quitting gracefully... (Reason: {reason})")
            else:
                LOGGER.info("Quitting gracefully...")
            
            if self.dedicatedserver and self.dedicatedserver.status in [ServerStatus.RUNNING, ServerStatus.STARTING]:
                # If no RCON is connected while running or starting, simply kill server
                if not self.dedicatedserver.rcon.connected:
                    self.dedicatedserver.kill()
                    return
                
                # If server is running, simply shut it down and return to let it finish normally
                LOGGER.debug("Shutting down Dedicated Server before quitting...")
                self.dedicatedserver.shutdown()
                return
            else:
                # If no server is running, exit directly
                LOGGER.info("Goodbye!")
                LOGGER.debug("Quitting with exit code 0...")
                sys.exit(0)
        else:
            if reason:
                LOGGER.info(f"Quitting... (Reason: {reason})")
            else:
                LOGGER.info("Quitting...")
            
            # Kill server if it's running or not
            if self.dedicatedserver:
                self.dedicatedserver.kill()
            
            # Give short time to send status update
            if self.status_thread:
                self.status_thread.update_status(status=False, message="Server was forcibly closed")
            
            time.sleep(0.1)
            
            sys.exit(1)

if __name__ == "__main__":
    # Exit directly, if python version below 3.9 is discovered
    if (sys.version_info.major < 3) or ((sys.version_info.major == 3) and (sys.version_info.minor < 9)):
        print()
        print("ERROR:   AstroTuxLauncher needs at least Python 3.9 to run properly!")
        print(f"        You are currently running version {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}.")
        print()
        sys.exit(1)
    
    # Parse command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("command", type=LauncherCommand, action=interface.EnumStoreAction, help=HELP_COMMAND)
    parser.add_argument("-c", "--config_path", help="The location of the configuration file (default: %(default)s)", type=str, dest="config_path", default="launcher.toml")
    parser.add_argument("-p", "--astro_path", help="The path of the Astroneer Dedicated Server installation (default: %(default)s)", dest="astro_path", default=None)
    parser.add_argument("-d", "--depotdl_exec", help="The path to anm existing depotdownloader executable (default: %(default)s)", dest="depotdl_exec", default=None)
    parser.add_argument("-l", "--log_debug", help="Also log debug messages (Overrules config option)", action='store_true', dest="log_debug", default=False)
    
    args = parser.parse_args()
    
    
    # Set terminal window title
    if CONTROL_CODES_SUPPORTED is None:
        set_window_title(f"{NAME} - Unofficial Astroneer Dedicated Server Launcher for Linux")
    
    # Print Banner
    print(BANNER_LOGO, end="")
    print(BANNER_SUBTITLE)
    print("")
    print(BANNER_TEXT)
    print(f"v{LAUNCHER_VERSION}")
    print("")

    if args.command == LauncherCommand.GENCONFIG:
        print("Generating config and exiting...")
        LauncherConfig.ensure_toml_config(path.abspath(args.config_path))
        sys.exit(0)
    
    try:
        launcher = AstroTuxLauncher(args.config_path, args.astro_path, args.depotdl_exec, force_debug_log=args.log_debug)
    except KeyboardInterrupt:
        print("Quitting... (requested by user)")
        sys.exit(0)
    except Exception as e:
        print(f"Error while initializing launcher on line {sys.exc_info()[-1].tb_lineno}: {type(e).__name__}: {e}")
        print(traceback.format_exc())
        print("Quitting...")
        sys.exit(1)
    
    signal.signal(signal.SIGINT, launcher.user_exit)
    
    if CONTROL_CODES_SUPPORTED == False:
        LOGGER.debug("ANSI escape codes except color codes are disabled")
    
    LOGGER.debug(f"CLI Command: {args.command.value}")
    
    if args.command == LauncherCommand.INSTALL:
        LOGGER.info("Installing Astroneer Dedicated Server...")
        try:
            launcher.update_server()
        except Exception as e:
            LOGGER.critical(f"Error while installing server on line {sys.exc_info()[-1].tb_lineno}: {type(e).__name__}: {e}")
            LOGGER.error(traceback.format_exc())
            sys.exit(1)
    elif args.command == LauncherCommand.UPDATE:
        LOGGER.info("Checking for available updates to the Astroneer Dedicated Server...")
        
        try:
            launcher.check_server_update(force_update=True)
        except Exception as e:
            LOGGER.critical(f"Error while updating server on line {sys.exc_info()[-1].tb_lineno}: {type(e).__name__}: {e}")
            LOGGER.error(traceback.format_exc())
            sys.exit(1)
    elif args.command == LauncherCommand.START:
        try:
            launcher.start_server()
        except Exception as e:
            if launcher.dedicatedserver:
                launcher.dedicatedserver.kill()
            
            raise
    
    LOGGER.info("Goodbye!")
