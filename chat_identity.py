"""Evidence-based name/city parsing. No fuzzy matching or guessed city aliases."""
import re
import unicodedata


def norm(value):
    return ' '.join(re.sub(r'[^a-zа-я0-9]+',' ',unicodedata.normalize('NFKC',value or '').casefold().replace('ё','е')).split())


CITIES = '''Омск|Электросталь|Гатчина|Ноябрьск|Иваново|Новокузнецк|Уфа|Балаково|Нижневартовск|Лангепас|Мегион|Белгород|Нижний Новгород|Кемерово|Самара|Оренбург|Ульяновск|Тверь|Южно-Сахалинск|Донецк|Обнинск|Орёл|Первоуральск|Саратов|Шадринск|Тобольск|Екатеринбург|Воронеж|Калуга|Стерлитамак|Москва|Краснодар|Абакан|Челябинск|Красноярск|Ростов-на-Дону|Нефтекамск|Белая Колитва|Орск|Курган|Пермь|Нижний Тагил|Симферополь|Тамбов|Новосибирск|Благовещенск|Севастополь|Когалым|Зеленодольск|Набережные Челны|Волгоград|Чернушка|Сочи|Владимир|Югорск|Калининград|Ставрополь|Улан-Удэ|Тула|Тольятти|Брянск|Казань|Новый Уренгой|Видное|Барнаул|Череповец|Мурманск|Альметьевск|Тюмень|Вологда|Владикавказ|Сургут|Анапа|Егорьевск|Дубна|Нижнекамск|Санкт-Петербург|Саранск|Чита|Курск|Иркутск|Рязань|Тихорецк|Тайшет|Канск|Ижевск|Архангельск|Салават|Таганрог|Апшеронск|Шахты|Майкоп|Астрахань|Междуреченск|Будённовск|Чебоксары|Истра|Нижегородская область|Зеленоград|Хабаровск|Елец'''.split('|')
ALIASES={norm(city):city for city in CITIES}
ALIASES.update({norm(city):city for city in ['Смоленск','Магнитогорск','Туапсе','Октябрьский','Троицк']})
ALIASES.update({'мск':'Москва','екб':'Екатеринбург','новосиб':'Новосибирск','спб':'Санкт-Петербург','наб челны':'Набережные Челны'})

SERVICE_PHRASES = '''местный маркетинг|а автовыгрузка|а автозагрузка|авито выгрузка|авито автозагрузка|it интегратор|нейро продавец|нейропродавец|автовыгрузка|автозагрузка|н п|нп|vk|вк|avito|авито|target|таргет|трафик|trafic|traffic|tg|тг|ads|як|б у'''.split('|')
SERVICE_RE=re.compile(r'(?<!\w)(?:'+'|'.join(re.escape(p) for p in sorted(SERVICE_PHRASES,key=len,reverse=True))+r')(?!\w)')
INTERNAL=re.compile(r'^(?:crm\b|скрипт|рассылк|контент|отработка|сливающ|слившиеся|техничка|база\b|банда\b|спецы\b|руководство\b|регламент|продажи\b|продления\b|тест\b|совет старейшин|автоматизация\b|креотивы\b|креативы\b|не взяли города|партнерка\b|местный маркетинг\b|трафик для франшиз\b)')


def parse(title):
    raw=norm(title)
    if INTERNAL.search(raw) or re.search(r'(?<!\w)(?:партнеры|патнеры|менеджер|тест)(?!\w)',raw):
        return {'kind':'internal','brand':None,'cities':[],'key':None}
    value=re.sub(r'(?<!\w)(?:avito|авито)\s+2(?!\w)',' ',raw)
    value=re.sub(r'(?<!\w)нейробот(?!\w)',' ',value)
    value=SERVICE_RE.sub(' ',value)
    value=' '.join(value.split())
    cities=[]
    # Long names first prevents Орск from matching inside another city name.
    for alias,canonical in sorted(ALIASES.items(),key=lambda item:len(item[0]),reverse=True):
        pattern=r'(?<!\w)'+re.escape(alias)+r'(?!\w)'
        if re.search(pattern,value):
            cities.append(canonical)
            value=re.sub(pattern,' ',value)
    value=' '.join(value.split())
    # A leading counter belongs to the service label in these observed titles.
    if re.match(r'^\d+\s*[-–—]\s*',title.casefold()) and 'нейро' in raw:
        value=re.sub(r'^\d+\s*','',value)
    # "9:41" is a brand, so digits are otherwise preserved.
    cities=sorted(set(cities),key=norm)
    if len(cities)>1:value=re.sub(r'(?<!\w)и(?!\w)',' ',value).strip()
    if 'ты в тренде' in value:value=value.replace('ты в тренде','').strip()
    if not cities:
        return {'kind':'no_city','brand':value or None,'cities':[],'key':None}
    if not value:
        return {'kind':'city_only','brand':None,'cities':cities,'key':None}
    # Unparsed modifiers require review rather than manufacturing a brand.
    if any(token in value.split() for token in ('old','нуля','область','нмск','с')):
        return {'kind':'needs_review','brand':value,'cities':cities,'key':None}
    brand=value
    return {'kind':'named','brand':brand,'cities':cities,
            'key':''.join(brand.split())+'::'+'+'.join(norm(c) for c in cities)}
