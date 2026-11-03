# Filing rules

Out of the box two naming templates (Settings > Naming) decide where every film and episode goes. Filing rules
replace them with an ordered list you edit in **Settings > Filing rules**: the first rule that fits an item files
it. Rules can sort by genre, language, country, collection, quality and more, keep collections together, turn a
second copy of a film into a version of the one already there, and send anything you want to see first to the
review page. Rules are saved in `data/config.yml` under `filing` and can be exported and imported as YAML.

## A rule

```yaml
filing:
  enabled: true
  policies: {quality_from: name-or-picture}
  rules:
    - name: Animation
      when: {type: film, genres: [Animation]}
      root: 'X:\Movies\Animation'
      folder: '{Title} ({Year}){ |Tags}'
      tags: ['{Quality}']
    - name: Japanese films
      when:
        type: film
        any: [{original_language: [ja]}, {countries: [JP]}]
        not: {dubbed: ja}
      root: 'X:\Movies\Japanese'
      folder: '{Title}{ - |TranslatedTitle:ja/JP} ({Year})'
    - name: Films
      when: {type: film}
      root: 'X:\Movies\Films'
      collections: {existing: true, create: true, min_released: 2}
    - name: Series
      when: {type: episode}
      root: 'X:\Series'
      show_folder: '{Title} ({Year})'
      season_folder: 'Season {Season:00}'
      episode_file: '{Series} {Code}{ - |EpisodeTitle}'
```

**Conditions** (all must hold; a list means any of its values): `type` (film, episode, special), `genres`,
`keywords`, `original_language`, `countries`, `audio_languages`, `subtitle_languages`, `dubbed` (a language code or
`any`: an audio track in that language that is not the original one, or a dub word in the release name),
`collection`, `quality` (the release name's 1080p / 720p / DVD word), `picture` (from the picture size), `year` and
`runtime` (`{min, max}`), `in_library`, `details` (the lookup's record is known), `name` (a regular expression on
the release name). `any: [...]` holds when one of its blocks holds; `not: {...}` when its block does not.

**Outputs**: `root`, `folder` and `file` for films; `show_folder`, `season_folder` and `episode_file` for episodes;
`tags` (a list; an entry can be `{text, when}` to add a tag only when its conditions hold); `collections`;
`hold: <reason>` sends what the rule fits to the review page instead of filing it.

**Tokens**: `{Title}` `{OriginalTitle}` `{TranslatedTitle:ja}` (the title in that language; `ja/JP` falls back to
the alternative title used in that country) `{Year}` `{Quality}` `{Picture}` `{Tags}` `{Collection}` `{Folder}`
`{Series}` (the show folder's name) `{Season:00}` `{Episode:00}` `{Code}` (S01E02-E03) `{EpisodeTitle}`. Write
`{ - |TranslatedTitle:ja}` for a part that shows only when the token has a value. Characters Windows does not allow
in names are replaced (`:` becomes ` -`).

## For every rule

Policies apply to all rules (a rule can override any of them under `policies`): where the quality word comes from;
a second copy of a film already filed (a version in its folder, named by `version_label`, or its own folder); a
copy whose audio differs; trailers (beside the film, or in a Trailers folder) and parts; the dubbed-language label
used to tell copies apart; whether copies wait on the review page; Latin titles first; and whether pictures that
came with a film get Jellyfin's names. Existing folders are found by the IDs in their folder-icon file names, then
by title and year; collection folders by their TMDB id or name.

`dub_words` are matched as whole words in any case; write one between slashes (`/ja[ ._-]?dub/`) for a regular
expression. A version with burnt-in subtitles is labelled by `hardsub_label` with the language word nearest to
"hardsub" in its name (`720p French Hardsub`); `hardsub_languages` limits which languages are named, and any other
hardsub is labelled `Hardsub`.

Which title `{Title}` is: `latin_titles` uses the original title when the title is not in Latin letters and the
original is; `original_title_for_scripts` (a list of `arabic`, `cyrillic`, `greek`, `hebrew`, `cjk`, `devanagari`,
`thai`) uses the original when the title has letters of one of those scripts and the original has none. When TMDB
lists several translations for one language, `translation_pick: first` (the default) uses the first with a title,
and `last` the last one listed; when that one has no title, the next part of the token (`{TranslatedTitle:ja/JP}`:
the country's alternative title) is tried.

Subtitles that come with a file are named `<name>.<language>[.forced|.sdh].<ext>` (a second one of a language
`.alt`), with the language from the file name or the text; picture subtitles go to `Other Files` with an `.ignore`
marker. Folder icons and full details files are hooks that read where the rules filed an item (see
[FOLDER-ICONS.md](FOLDER-ICONS.md) and [HOOKS.md](HOOKS.md)).

## Updates and rules

The update host step asks a new version whether it understands your rules before it takes over; a version that
does not know a condition or policy you use is refused (see [UPDATES.md](UPDATES.md)).
