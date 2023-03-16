import csv
import io
import os
import re
import sqlite3

import click
import openai

PROMPT = """
You are MTGBot. You have expert level experience with Magic: The Gathering competitive drafting.
You will be generating a very high quality article in the voice of MTG Hall of Famer Reid Duke.
I'm going to give you a list of all the cards of one color from a recent expansion set.
Your task is to parse the data and:
    * determine if it would be viable to play a mono-color deck of that color (be realistic!)
      * remember the typical optimal distribution of card types:
        * 17 lands
        * 15 creatures
        * 8 non-creature spells
      * be willing to bend the typical distribution
    * what the general theme of the deck might be
      * if there are multiple options, mention it
    * some key cards to look for
      * dont just focus on rare and mythic cards
    * interesting combinations of cards
      * look for unexpected synergy that other players might miss and thus will be under-drafted
    * think about cards that are more useful in a mono-color deck
    * dont forget a win condition

Your output should include:
    * a quick brainstorming session based on the cards you just saw
    * once you understand all the cards and how they might work together, write the article
      in the format of a blog post

Good luck, you'll do great!

Your input:
"""

SUPER_TYPE_MAPPING = {
    'Artifact': 'A',
    'Aura': 'A2',
    'Creature': 'C',
    'Enchantment': 'E',
    'Instant': 'I',
    'Interrupt': 'I2',
    'Land': 'L',
    'Legendary': 'L2',
    'Planeswalker': 'P',
    'Sorcery': 'S',
}

SUB_TYPE_MAPPING = {
    'Advisor': 'Ad',
    'Angel': 'An',
    'Artificer': 'Ar',
    'Assassin': 'As',
    'Barbarian': 'Ba',
    'Beast': 'Be',
    'Bird': 'Bi',
    'Cleric': 'Cl',
    'Construct': 'Co',
    'Demon': 'De',
    'Dinosaur': 'Di',
    'Dragon': 'Dr',
    'Dreadnought': 'Dr2',
    'Druid': 'Dr3',
    'Dwarf': 'Dw',
    'Elemental': 'El',
    'Equipment': 'Eq',
    'Faerie': 'Fa',
    'Goblin': 'Go',
    'Golem': 'Go2',
    'Horror': 'Ho',
    'Human': 'Hu',
    'Insect': 'In',
    'Juggernaut': 'Ju',
    'Lizard': 'Li',
    'Minotaur': 'Mi',
    'Mole': 'Mo',
    'Monk': 'Mo2',
    'Phyrexian': 'Ph',
    'Praetor': 'Pr',
    'Rat': 'Ra',
    'Scout': 'Sc',
    'Shapeshifter': 'Sh',
    'Soldier': 'So',
    'Thopter': 'Th',
    'Wall': 'Wa',
    'Warlock': 'Wa2',
    'Wizard': 'Wi',
    'Wurm': 'Wu',
    'Zombie': 'Zo',
}

LAND_MAPPING = {
    'Basic': 'BaL',

    'Forest': 'FoL',
    'Island': 'IsL',
    'Mountain': 'MoL',
    'Plains': 'PlL',
    'Swamp': 'SwL',
}

RARITY_MAPPING = {
    'common': 'C',
    'uncommon': 'U',
    'rare': 'R',
    'mythic': 'M',
}

KEYWORD_MAPPING = {
    'Deathtouch': 'DTH',
    'Flying': 'FLY',
    'Haste': 'HST',
    'Lifelink': 'LLK',
    'Menace': 'MEN',
    'Trample': 'TRP',
    'Vigilance': 'VIG',
    'Exile': 'EXL',
}

NUMBER_MAPPING = {
    'One': '1',
    'Two': '2',
    'Three': '3',
    'Four': '4',

    'First': '1st',
    'Second': '2nd',
    'Third': '3rd',
}


TEXT_MAPPING = {
    "\n": " ",
    ' — ': '—',
    '+1/+1': 'PP',

    'At the beginning': 'ATB',
    'Combat': 'CBT',
    'Counter': 'CT',
    'Create': 'CTE',
    'Creature': 'CR',
    'Destroy target': 'DT',
    'Double strike': 'DS',
    'Draw a card': 'DAC',
    'Enchant creature': 'EC',
    'End of turn': 'EOT',
    'Enters the battlefield': 'ETB',
    'Exile target': 'EXT',
    'Leaves the battlefield': 'LTB',
    'Mana value': 'MV',
    'Permanent': 'PM',
    'Player': 'PYR',
    'Return target': 'RT',
    'Scry': 'SC',
    'Search your library': 'SYL',
    'Surveil': 'SU',
    'Target': 'TR',
    'Token': 'TKN',
    'Whenever': 'WN',
    'You control': 'YC',
    'You gain': 'YG',
}

MANA_COST_MAPPING = {
    '{': '',
    '}': '',
}


@click.command()
@click.option(
    "--set-code",
    required=True,
    help="Magic: The Gathering Set Code",
)
def main(set_code):
    openai.api_key = get_api_key()

    set_data, cards = load_mtg_set("./AllPrintings.sqlite", set_code)
    gpt_prompt = generate_prompt(set_data, cards)

    print(gpt_prompt)


def get_api_key():
    return os.environ["MTG_BOT_OPENAI_API_KEY"]


def load_mtg_set(path, set_code):
    conn = sqlite3.connect(path)
    cursor = conn.cursor()

    # should only be one
    for (name, release, set_type) in cursor.execute(
            """
            SELECT
                name,
                releaseDate,
                type
            FROM
                sets
            WHERE
                code = ?
            LIMIT 1;
            """,
            (set_code,),
    ):
        set_data = {
            'name': name,
            'release': release,
            'type': set_type,
        }

    cards = []

    for (
            artist,
            card_type,
            colors,
            flavor_text,
            is_full_art,
            is_reprint,
            mana_cost,
            mana_value,
            name,
            number,
            original_release_date,
            power,
            rarity,
            text,
            toughness,
    ) in cursor.execute(
            """
            SELECT
                artist,
                type,
                colors,
                flavorText,
                isFullArt,
                isReprint,
                manaCost,
                manaValue,
                name,
                number,
                originalReleaseDate,
                power,
                rarity,
                text,
                toughness
                FROM
                    cards
                WHERE
                    setCode = ?
                AND
                    (
                        colors = 'U'
                    );
            """,
            (set_code,),
    ):

        # apply abbreviations - card type - super
        for from_str, to_str in SUPER_TYPE_MAPPING.items():
            card_type = card_type.replace(from_str, to_str)
            card_type = card_type.replace(from_str.lower(), to_str)

        # apply abbreviations - card type - sub
        for from_str, to_str in SUB_TYPE_MAPPING.items():
            card_type = card_type.replace(from_str, to_str)
            card_type = card_type.replace(from_str.lower(), to_str)

        # apply abbreviations - card type - land
        for from_str, to_str in LAND_MAPPING.items():
            card_type = card_type.replace(from_str, to_str)
            card_type = card_type.replace(from_str.lower(), to_str)

        # apply abbreviations - text - text
        for from_str, to_str in TEXT_MAPPING.items():
            text = text.replace(from_str, to_str)
            text = text.replace(from_str.lower(), to_str)

        # apply abbreviations - text - super
        for from_str, to_str in SUPER_TYPE_MAPPING.items():
            text = text.replace(from_str, to_str)
            text = text.replace(from_str.lower(), to_str)

        # apply abbreviations - text - sub
        for from_str, to_str in SUB_TYPE_MAPPING.items():
            text = text.replace(from_str, to_str)
            text = text.replace(from_str.lower(), to_str)

        # apply abbreviations - text - land
        for from_str, to_str in LAND_MAPPING.items():
            text = text.replace(from_str, to_str)
            text = text.replace(from_str.lower(), to_str)

        # apply abbreviations - text - keywords
        for from_str, to_str in KEYWORD_MAPPING.items():
            text = text.replace(from_str, to_str)
            text = text.replace(from_str.lower(), to_str)

        # apply abbreviations - text - numbers
        for from_str, to_str in NUMBER_MAPPING.items():
            text = text.replace(from_str, to_str)
            text = text.replace(from_str.lower(), to_str)

        # apply abbreviations - text - mana cost
        for from_str, to_str in MANA_COST_MAPPING.items():
            text = text.replace(from_str, to_str)

        # remove explaination text
        text = re.sub(r'\(.*\)', '', text)

        # replace card name in text with placeholder
        text = text.replace(name, 'NAME')

        # clean up any extra whitespace
        text = re.sub(r'\s+', ' ', text.strip())

        # replace names of basic lands
        for from_str, to_str in LAND_MAPPING.items():
            name = name.replace(from_str, to_str)

        # apply abbreviations - rarity
        rarity = RARITY_MAPPING[rarity]

        # strip braces from mana cost
        if mana_cost:
            for from_str, to_str in MANA_COST_MAPPING.items():
                mana_cost = mana_cost.replace(from_str, to_str)

        cards.append({
            'artist': artist,
            'type': card_type,
            'colors': colors,
            'flavor_text': flavor_text,
            'is_full_art': is_full_art,
            'is_reprint': is_reprint,
            'mana_cost': mana_cost,
            'mana_value': mana_value,
            'name': name,
            'number': number,
            'original_release_date': original_release_date,
            'power': power,
            'rarity': rarity,
            'text': text,
            'toughness': toughness,
        })

    return (set_data, cards)


def generate_prompt(set_data, cards):
    set_explanation = explain_set(set_data)
    cards_explanation = explain_cards(cards)

    return f"{set_explanation}{cards_explanation}"


def explain_set(set_data):
    output = io.StringIO()

    writer = csv.DictWriter(
        output,
        fieldnames=[
            'name',
            'release',
            'type',
        ],
        delimiter='|',
        lineterminator="\n",
    )

    writer.writeheader()
    writer.writerow(set_data)

    return output.getvalue()


def explain_cards(cards):
    output = io.StringIO()

    writer = csv.DictWriter(
        output,
        fieldnames=[
            'type',
            'mana_cost',
            'name',
            'power',
            'toughness',
            'rarity',
            'text',
        ],
        delimiter='|',
        extrasaction='ignore',
        lineterminator="\n",
    )

    writer.writeheader()

    for card in cards:
        writer.writerow(card)

    return output.getvalue()


if __name__ == "__main__":
    main()
