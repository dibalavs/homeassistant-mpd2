#import mpd
#from mpd.asyncio import MPDClient
from mpd import MPDClient

client = MPDClient()               # create client object
client.timeout = 10                # network timeout in seconds (floats allowed), default: None
client.idletimeout = None          # timeout for fetching the result of the idle command is handled seperately, default: None
client.connect("localhost", 6600)  # connect to localhost:6600
#print(client.listmounts())
print(client.lsinfo())
print()
print(client.tagtypes())
print()
print(client.tagtypes())
print()
print(client.list("Title"))
print()
print(client.find("Album", 'Знаменитая собачка Соня'))