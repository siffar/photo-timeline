# Photo Timeline

**All your scattered photos (Google Takeout, old phones, every external drive) in one timeline,
with trips and places. No server, no cloud, no subscription.**

Photo Timeline is a small app that runs on your own computer and opens in your browser. Point it
at your drives and folders and it puts every photo and video on the day it was actually taken,
hides the duplicates, finds your trips, and lets you search by place or by what's in the photo.

- 🔒 **Private:** everything runs on your computer. Nothing is uploaded.
- 📁 **Read-only:** your original files are only ever read. They're never moved, renamed or deleted.
- 🧳 **Built for messy collections:** Google Takeout `.json` files, compressed "storage saver" copies,
  iPhone HEIC and Live Photos, camera RAW, WhatsApp folders, several drives at once.

> **Status:** early release, used daily on macOS. Windows support is built in and is being tested;
> please [open an issue](../../issues) if something doesn't work.

## Install and start

1. Install **Python 3.9 or newer** from <https://www.python.org/downloads/>.
   On Windows, tick **“Add python.exe to PATH”** in the installer.
2. Download this project (green **Code** button → **Download ZIP**, or the latest
   [release](../../releases)), unzip it, and double-click **Start Photo Timeline**:
   - **Start Photo Timeline.bat** on Windows
   - **Start Photo Timeline.command** on Mac

   The first start installs what it needs, which takes about a minute. On Windows it also offers to
   install ffmpeg for video thumbnails.
3. Your browser opens. Tick the drives and folders that hold your photos, then press **Add & scan**.

**First launch warnings:** the launchers aren't signed, so your computer will ask once:
- **Mac:** "Apple could not verify…". Right-click the file → **Open**, or go to
  System Settings → Privacy & Security → **Open Anyway**. In Terminal you can also run
  `xattr -dr com.apple.quarantine photo-timeline`.
- **Windows:** "Windows protected your PC". Click **More info → Run anyway**.

That's all. Next time, double-click the same file. It automatically picks up any new photos
on drives that are plugged in.

> Keep the black window open while you use the app; closing it stops Photo Timeline.

## What it does

**Finds the real date** of every photo and video. It tries these in order:

1. the date stored inside the file by the phone or camera
2. Google Takeout's `.json` files, for photos *and* videos
3. the file name, e.g. `IMG_20230514_…`, `PXL_…` or WhatsApp's `IMG-20210820-WA0001`
4. the file's modified time, as a last resort; these are listed under **Needs review**

From the Google `.json` files it also reads location, captions, people tags and favourites. It
copes with all of Takeout's odd naming: `.supplemental-metadata.json`, cut-off names, `(1)` copies
and `-edited` copies.

**Hides duplicates** without deleting anything:
- exact copies, such as the same photo on two drives
- look-alike copies, such as a compressed Google Photos version of your iPhone original; the larger one is kept

**Skips junk.** Windows, Program Files, AppData, app caches, `node_modules`, thumbnails folders
and icon-sized images are ignored, so ticking a whole drive is fine.

**Gallery**
- photos laid out by month and day, with a year bar for jumping around
- search by year, month, camera, file name, or a person's name from Google Photos
- filters by folder, camera and photo/video
- full-screen viewer with slideshow, swipe on phones, and keyboard shortcuts: ← → Esc, `F` to favourite, `I` for details, Space for slideshow

**Pages:** Favourites, On this day, Map, Duplicates, Needs review.

**Live Photos:** each iPhone photo is joined to its motion clip. Press ● Live to play it.

**Google Photos albums** in your Takeout appear under Albums automatically.

## Trips, events and places

- **Trips** are made automatically from your photo locations: the times you were away from your
  usual places (home, college, office), e.g. “Manali · 4 days · Dec 2023” or “Kathmandu, Nepal”.
  Your usual places are worked out over time, so a move (say Manipal → Delhi) is handled.
  Camera photos without GPS (e.g. a Nikon) join a trip if they were taken on its days.
  Each trip page has a route map. Rename a trip and your name is kept; **Save as my album** makes
  a fixed copy you can edit.
- **Events** are unusually busy days at home (weddings, birthdays). Rename them to whatever they were.
- **Places** — browse Country → State → City, and search by place (“goa”, “ladakh”, “kasol”).
  Place names are worked out on your computer from the GPS in your photos. The place list
  (GeoNames, every town with 1,000+ people) is downloaded once, about 10 MB, during your first
  scan; after that it works offline.

## Things — what's in your photos (Mac)

On a Mac, Photo Timeline can recognise what's in each photo using **Apple's built-in image
recognition** (the same engine the Photos app uses), on the Mac's own AI chip:

- Search for *mountains*, *beach*, *snow*, *sunset*, *dog*, *food*, *bicycle*… and for **text inside
  photos** (screenshots, tickets, signboards).
- Browse everything on the **Things** page, plus **People** (photos with faces) and **Text & screenshots**.
- Light: runs at low priority on two background workers, using the small thumbnails the app already
  made — roughly 5–10 minutes per 50,000 photos the first time, then only new photos after each scan.
- Nothing is uploaded and no AI models are downloaded; the only download is a ~15 MB connector
  (`pyobjc-framework-Vision`), which the app installs when you press **Turn on smart tags**.
- It finds *that* there are people in a photo, not *who* they are.

## Pick, copy and share

- **Select:** hover over a photo and tick its circle. Tick a day's heading to select the whole day.
  Shift-click selects a range, and Ctrl+A selects everything in view.
- With photos selected, you can:
  - **♥ Favourite** them
  - **＋ Add to album**
  - **⧉ Copy to folder** (originals, full quality, with the date added to file names if you want)
  - **⬇ Download .zip**
- An album can be shared in two ways:
  - **Share on Wi-Fi**: a link and a QR code that friends on the same Wi-Fi can open on their
    phones to view and download. It works while the app is open. If Windows asks about network
    access, choose *Private networks*.
  - **Make a folder to send**: creates a folder and a **.zip** containing a small photo page.
    Send the .zip any way you like (WhatsApp, Drive, WeTransfer). Friends unzip it and open
    `index.html` in any browser. No app or internet needed.
    - Choose **Quick to share** for small, resized files.
    - Choose **Full quality** to include the originals too.

## Good to know

- Unplugged drives are fine. Their photos stay in your timeline with previews, and the full-size
  original opens again when the drive is plugged back in.
- The `library` folder next to the app holds the index and thumbnails. Deleting it just means the
  next scan starts from scratch.
- Some newer iPhone videos use HEVC, which may not play inside Chrome on Windows. Press ⬇ to
  download one, or use **ⓘ → Show in folder**. Shared albums made with ffmpeg installed
  convert videos so they play everywhere.
- The map and trip routes load map tiles from OpenStreetMap, so they need internet. Everything else
  works offline.

## Command line (optional)

```
python timeline.py                                   # the app
python timeline.py scan "E:\" Google="D:\Takeout"    # add & scan without the browser
python timeline.py organize --out "D:\Photos Sorted" # copy everything (no duplicates) into YEAR\YEAR-MONTH folders
```

Options:
- `--port 8765`
- `--library <folder>`
- `--no-browser`
- `--no-autoscan`
- `scan --tz +05:30`: the time zone used for Google and video timestamps; by default, this computer's

## Updating

Download the new version and unzip it over your existing folder. Your `library` folder (your
index, albums, favourites and trip names) isn't part of the download, so it's kept. On a Mac:

```
cd ~/Downloads && unzip -o photo-timeline.zip && cd photo-timeline && python3 timeline.py
```

## Contributing

Bug reports and ideas are welcome: please [open an issue](../../issues). Include your operating
system and, if something failed, the text shown in the black/Terminal window.

## Credits

Photo Timeline is built on these projects and data, with thanks:

- Place names from [GeoNames](https://www.geonames.org/), licensed under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- Maps © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, displayed with
  [Leaflet](https://leafletjs.com/) and Leaflet.markercluster
- [Pillow](https://python-pillow.org/), [pillow-heif](https://github.com/bigcat88/pillow_heif),
  [rawpy](https://github.com/letmaik/rawpy) / LibRaw, [ExifRead](https://github.com/ianare/exif-py),
  [PyObjC](https://pyobjc.readthedocs.io/), [certifi](https://github.com/certifi/python-certifi)
- [FFmpeg](https://ffmpeg.org/) (optional, installed separately) for video thumbnails
- Fonts: [Fraunces](https://fonts.google.com/specimen/Fraunces) and
  [Inter](https://fonts.google.com/specimen/Inter) (SIL Open Font License)

Each of these keeps its own licence.

## Licence

[MIT](LICENSE) © 2026 Anatyash Sachan
