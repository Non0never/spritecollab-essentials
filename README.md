# SpriteCollab for Pokémon Essentials

The repo checks SpriteCollab every six hours and publishes a new pack when something changes. Each release lists new, updated and removed graphics.

**[Download](https://github.com/Non0never/spritecollab-essentials/releases/latest)**

Grab `PMD-Sprites-Essentials-Complete.zip` and copy the `Graphics` folder into your game. The pack includes all available forms, shinies, gender variants, portraits and animations, sorted into folders.

Walking sprites use the usual 4×4 layout. Other animations use Essentials' `[N,D]` format. The full original files are included as a separate download.

The full list of changes is in `changes.md`. You can also start a build under **Actions → Update sprites**. GitHub may pause scheduled builds after 60 days without activity.

To convert the files yourself:

```sh
pip install -r requirements.txt
python convert.py /path/to/SpriteCollab /path/to/export
```

Artwork by the SpriteCollab contributors. Their credits and licenses are included in the downloads. Conversion by Nononever.
