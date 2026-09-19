"""
Zimbabwe species reference list (spec v1.5 §E).

Every entry is a species that genuinely occurs in Zimbabwe (resident, regular migrant or
well-established introduction). Tuples are::

    (common_name, scientific_name, shona_name, ndebele_name, iucn_status, taxon_group)

* ``scientific_name`` is the primary key material: :data:`field.models.SPECIES_NAMESPACE` +
  ``uuid5(scientific_name.lower())`` gives the same id on every server, so **never rename an
  existing entry** — add a new row instead. The names of the original 35 entries are therefore kept
  exactly as first published even where taxonomy has since moved on (e.g. the African Fish Eagle is
  now usually placed in *Icthyophaga*).
* ``iucn_status`` is the *global* IUCN Red List category (LC/NT/VU/EN/CR/EW/EX/DD/NE), not a
  national one.
* ``taxon_group`` is one of mammal | bird | reptile | amphibian | fish | invertebrate.
* Shona / Ndebele names are filled **only where attested with confidence** and left ``""``
  otherwise — vernacular names are never guessed (spec v1.5 §E). Have additions verified by field
  staff before relying on them.

The list is applied to the database by ``field/migrations/0004_species_catalogue.py`` (upsert, so
production gets it without ``seed_demo``) and bundled into the Android app by
``android/tools/gen_species_asset.py``.
"""

SPECIES = [
    # ---- mammals: mega-herbivores and large game -------------------------------------------------
    ("African Savanna Elephant", "Loxodonta africana", "Nzou", "Indlovu", "EN", "mammal"),
    ("Black Rhinoceros", "Diceros bicornis", "Chipembere", "Ubhejane", "CR", "mammal"),
    ("White Rhinoceros", "Ceratotherium simum", "Chipembere", "Umkhombe", "NT", "mammal"),
    ("Hippopotamus", "Hippopotamus amphibius", "Mvuu", "Imvubu", "VU", "mammal"),
    ("Giraffe", "Giraffa camelopardalis", "Twiza", "Intundla", "VU", "mammal"),
    ("African Buffalo", "Syncerus caffer", "Nyati", "Inyathi", "NT", "mammal"),
    ("Plains Zebra", "Equus quagga", "Mbizi", "Idube", "NT", "mammal"),
    ("Common Warthog", "Phacochoerus africanus", "Njiri", "", "LC", "mammal"),
    ("Bushpig", "Potamochoerus larvatus", "Humba", "", "LC", "mammal"),

    # ---- mammals: carnivores ---------------------------------------------------------------------
    ("Lion", "Panthera leo", "Shumba", "Isilwane", "VU", "mammal"),
    ("Leopard", "Panthera pardus", "Mbada", "Ingwe", "VU", "mammal"),
    ("Cheetah", "Acinonyx jubatus", "Dindingwe", "Ihlosi", "VU", "mammal"),
    ("African Wild Dog", "Lycaon pictus", "Mhumhi", "Inkentshane", "EN", "mammal"),
    ("Spotted Hyena", "Crocuta crocuta", "Bere", "Impisi", "LC", "mammal"),
    ("Brown Hyena", "Parahyaena brunnea", "", "", "NT", "mammal"),
    ("Aardwolf", "Proteles cristata", "", "", "LC", "mammal"),
    ("Side-striped Jackal", "Lupulella adusta", "Gava", "Ikhanka", "LC", "mammal"),
    ("Black-backed Jackal", "Lupulella mesomelas", "", "", "LC", "mammal"),
    ("Bat-eared Fox", "Otocyon megalotis", "", "", "LC", "mammal"),
    ("Caracal", "Caracal caracal", "", "", "LC", "mammal"),
    ("Serval", "Leptailurus serval", "", "", "LC", "mammal"),
    ("African Wildcat", "Felis lybica", "", "", "LC", "mammal"),
    ("Honey Badger", "Mellivora capensis", "", "Insele", "LC", "mammal"),
    ("African Civet", "Civettictis civetta", "", "", "LC", "mammal"),
    ("Small-spotted Genet", "Genetta genetta", "", "", "LC", "mammal"),
    ("Rusty-spotted Genet", "Genetta maculata", "", "", "LC", "mammal"),
    ("Banded Mongoose", "Mungos mungo", "", "", "LC", "mammal"),
    ("Common Dwarf Mongoose", "Helogale parvula", "", "", "LC", "mammal"),
    ("Slender Mongoose", "Galerella sanguinea", "", "", "LC", "mammal"),
    ("White-tailed Mongoose", "Ichneumia albicauda", "", "", "LC", "mammal"),
    ("Marsh Mongoose", "Atilax paludinosus", "", "", "LC", "mammal"),
    ("Meller's Mongoose", "Rhynchogale melleri", "", "", "LC", "mammal"),
    ("Selous's Mongoose", "Paracynictis selousi", "", "", "LC", "mammal"),
    ("Yellow Mongoose", "Cynictis penicillata", "", "", "LC", "mammal"),
    ("Striped Polecat", "Ictonyx striatus", "", "", "LC", "mammal"),
    ("African Striped Weasel", "Poecilogale albinucha", "", "", "LC", "mammal"),
    ("African Clawless Otter", "Aonyx capensis", "", "", "NT", "mammal"),
    ("Spotted-necked Otter", "Hydrictis maculicollis", "", "", "NT", "mammal"),

    # ---- mammals: antelope and other ungulates ---------------------------------------------------
    ("Greater Kudu", "Tragelaphus strepsiceros", "Nhoro", "", "LC", "mammal"),
    ("Nyala", "Tragelaphus angasii", "", "Inyala", "LC", "mammal"),
    ("Bushbuck", "Tragelaphus sylvaticus", "Dzoma", "Imbabala", "LC", "mammal"),
    ("Common Eland", "Taurotragus oryx", "Mhofu", "Impofu", "LC", "mammal"),
    ("Sable Antelope", "Hippotragus niger", "Mharapara", "Impalampala", "LC", "mammal"),
    ("Roan Antelope", "Hippotragus equinus", "", "", "LC", "mammal"),
    ("Impala", "Aepyceros melampus", "Mhara", "Impala", "LC", "mammal"),
    ("Blue Wildebeest", "Connochaetes taurinus", "", "Inkonkoni", "LC", "mammal"),
    ("Tsessebe", "Damaliscus lunatus", "", "", "LC", "mammal"),
    ("Lichtenstein's Hartebeest", "Alcelaphus lichtensteinii", "", "", "LC", "mammal"),
    ("Waterbuck", "Kobus ellipsiprymnus", "", "", "LC", "mammal"),
    ("Southern Reedbuck", "Redunca arundinum", "", "", "LC", "mammal"),
    ("Mountain Reedbuck", "Redunca fulvorufula", "", "", "EN", "mammal"),
    ("Common Duiker", "Sylvicapra grimmia", "Mhembwe", "Impunzi", "LC", "mammal"),
    ("Blue Duiker", "Philantomba monticola", "", "", "LC", "mammal"),
    ("Natal Red Duiker", "Cephalophus natalensis", "", "", "LC", "mammal"),
    ("Sharpe's Grysbok", "Raphicerus sharpei", "", "", "LC", "mammal"),
    ("Steenbok", "Raphicerus campestris", "", "Iqhina", "LC", "mammal"),
    ("Klipspringer", "Oreotragus oreotragus", "Ngururu", "", "LC", "mammal"),
    ("Oribi", "Ourebia ourebi", "", "", "LC", "mammal"),
    ("Suni", "Nesotragus moschatus", "", "", "LC", "mammal"),

    # ---- mammals: primates -----------------------------------------------------------------------
    ("Chacma Baboon", "Papio ursinus", "Gudo", "Indwangu", "LC", "mammal"),
    ("Vervet Monkey", "Chlorocebus pygerythrus", "Shoko", "Inkawu", "LC", "mammal"),
    ("Samango Monkey", "Cercopithecus albogularis", "", "", "LC", "mammal"),
    ("Thick-tailed Greater Galago", "Otolemur crassicaudatus", "", "", "LC", "mammal"),
    ("Southern Lesser Galago", "Galago moholi", "", "", "LC", "mammal"),

    # ---- mammals: small mammals ------------------------------------------------------------------
    ("Temminck's Ground Pangolin", "Smutsia temminckii", "Haka", "Inkakha", "VU", "mammal"),
    ("Aardvark", "Orycteropus afer", "", "Isambane", "LC", "mammal"),
    ("Cape Porcupine", "Hystrix africaeaustralis", "Nungu", "Inungu", "LC", "mammal"),
    ("South African Springhare", "Pedetes capensis", "", "", "LC", "mammal"),
    ("Scrub Hare", "Lepus saxatilis", "Tsuro", "Umvundla", "LC", "mammal"),
    ("Cape Hare", "Lepus capensis", "", "", "LC", "mammal"),
    ("Jameson's Red Rock Rabbit", "Pronolagus randensis", "", "", "LC", "mammal"),
    ("Smith's Red Rock Rabbit", "Pronolagus rupestris", "", "", "LC", "mammal"),
    ("Rock Hyrax", "Procavia capensis", "Mbira", "Imbila", "LC", "mammal"),
    ("Yellow-spotted Rock Hyrax", "Heterohyrax brucei", "", "", "LC", "mammal"),
    ("Southern Tree Hyrax", "Dendrohyrax arboreus", "", "", "LC", "mammal"),
    ("Smith's Bush Squirrel", "Paraxerus cepapi", "", "", "LC", "mammal"),
    ("Mutable Sun Squirrel", "Heliosciurus mutabilis", "", "", "LC", "mammal"),
    ("Woodland Dormouse", "Graphiurus murinus", "", "", "LC", "mammal"),
    ("Greater Cane Rat", "Thryonomys swinderianus", "", "", "LC", "mammal"),
    ("Southern Giant Pouched Rat", "Cricetomys ansorgei", "", "", "LC", "mammal"),
    ("Natal Multimammate Mouse", "Mastomys natalensis", "", "", "LC", "mammal"),
    ("Mesic Four-striped Grass Rat", "Rhabdomys dilectus", "", "", "LC", "mammal"),
    ("Angoni Vlei Rat", "Otomys angoniensis", "", "", "LC", "mammal"),
    ("Southern African Hedgehog", "Atelerix frontalis", "", "", "LC", "mammal"),
    ("Four-toed Sengi", "Petrodromus tetradactylus", "", "", "LC", "mammal"),
    ("Eastern Rock Sengi", "Elephantulus myurus", "", "", "LC", "mammal"),
    ("Short-snouted Sengi", "Elephantulus brachyrhynchus", "", "", "LC", "mammal"),
    ("Greater Musk Shrew", "Crocidura flavescens", "", "", "LC", "mammal"),
    ("Egyptian Rousette", "Rousettus aegyptiacus", "", "", "LC", "mammal"),
    ("Straw-coloured Fruit Bat", "Eidolon helvum", "", "", "VU", "mammal"),
    ("Wahlberg's Epauletted Fruit Bat", "Epomophorus wahlbergi", "", "", "LC", "mammal"),
    ("Mauritian Tomb Bat", "Taphozous mauritianus", "", "", "LC", "mammal"),
    ("Egyptian Slit-faced Bat", "Nycteris thebaica", "", "", "LC", "mammal"),
    ("Angolan Free-tailed Bat", "Mops condylurus", "", "", "LC", "mammal"),

    # ---- birds: ratites, raptors and vultures ----------------------------------------------------
    ("Common Ostrich", "Struthio camelus", "", "", "LC", "bird"),
    ("Secretarybird", "Sagittarius serpentarius", "", "", "EN", "bird"),
    ("White-backed Vulture", "Gyps africanus", "Gora", "Inqe", "CR", "bird"),
    ("Cape Vulture", "Gyps coprotheres", "", "", "VU", "bird"),
    ("Hooded Vulture", "Necrosyrtes monachus", "", "", "CR", "bird"),
    ("Lappet-faced Vulture", "Torgos tracheliotos", "", "", "EN", "bird"),
    ("White-headed Vulture", "Trigonoceps occipitalis", "", "", "CR", "bird"),
    ("Palm-nut Vulture", "Gypohierax angolensis", "", "", "LC", "bird"),
    ("African Fish Eagle", "Haliaeetus vocifer", "Hungwe", "", "LC", "bird"),
    ("Martial Eagle", "Polemaetus bellicosus", "", "", "EN", "bird"),
    ("Bateleur", "Terathopius ecaudatus", "", "", "EN", "bird"),
    ("Tawny Eagle", "Aquila rapax", "", "", "VU", "bird"),
    ("Steppe Eagle", "Aquila nipalensis", "", "", "EN", "bird"),
    ("Verreaux's Eagle", "Aquila verreauxii", "", "", "LC", "bird"),
    ("African Hawk-Eagle", "Aquila spilogaster", "", "", "LC", "bird"),
    ("Wahlberg's Eagle", "Hieraaetus wahlbergi", "", "", "LC", "bird"),
    ("Brown Snake Eagle", "Circaetus cinereus", "", "", "LC", "bird"),
    ("Black-chested Snake Eagle", "Circaetus pectoralis", "", "", "LC", "bird"),
    ("African Harrier-Hawk", "Polyboroides typus", "", "", "LC", "bird"),
    ("Peregrine Falcon", "Falco peregrinus", "", "", "LC", "bird"),
    ("Lanner Falcon", "Falco biarmicus", "", "", "LC", "bird"),
    ("Taita Falcon", "Falco fasciinucha", "", "", "VU", "bird"),
    ("Amur Falcon", "Falco amurensis", "", "", "LC", "bird"),
    ("Verreaux's Eagle-Owl", "Bubo lacteus", "", "", "LC", "bird"),
    ("Spotted Eagle-Owl", "Bubo africanus", "", "", "LC", "bird"),
    ("Pel's Fishing Owl", "Scotopelia peli", "", "", "LC", "bird"),
    ("African Barred Owlet", "Glaucidium capense", "", "", "LC", "bird"),

    # ---- birds: large terrestrial, waterbirds and cranes -----------------------------------------
    ("Southern Ground Hornbill", "Bucorvus leadbeateri", "Dendera", "Intsingizi", "VU", "bird"),
    ("Kori Bustard", "Ardeotis kori", "", "", "NT", "bird"),
    ("Denham's Bustard", "Neotis denhami", "", "", "NT", "bird"),
    ("Red-crested Korhaan", "Lophotis ruficrista", "", "", "LC", "bird"),
    ("Wattled Crane", "Bugeranus carunculatus", "", "", "VU", "bird"),
    ("Grey Crowned Crane", "Balearica regulorum", "", "", "EN", "bird"),
    ("Marabou Stork", "Leptoptilos crumenifer", "", "", "LC", "bird"),
    ("Saddle-billed Stork", "Ephippiorhynchus senegalensis", "", "", "LC", "bird"),
    ("Yellow-billed Stork", "Mycteria ibis", "", "", "LC", "bird"),
    ("African Openbill", "Anastomus lamelligerus", "", "", "LC", "bird"),
    ("Hamerkop", "Scopus umbretta", "", "", "LC", "bird"),
    ("Goliath Heron", "Ardea goliath", "", "", "LC", "bird"),
    ("Great White Pelican", "Pelecanus onocrotalus", "", "", "LC", "bird"),
    ("African Skimmer", "Rynchops flavirostris", "", "", "NT", "bird"),
    ("African Jacana", "Actophilornis africanus", "", "", "LC", "bird"),
    ("Egyptian Goose", "Alopochen aegyptiaca", "", "", "LC", "bird"),
    ("Spur-winged Goose", "Plectropterus gambensis", "", "", "LC", "bird"),
    ("Knob-billed Duck", "Sarkidiornis melanotos", "", "", "LC", "bird"),
    ("White-faced Whistling Duck", "Dendrocygna viduata", "", "", "LC", "bird"),
    ("Hadada Ibis", "Bostrychia hagedash", "", "", "LC", "bird"),
    ("African Sacred Ibis", "Threskiornis aethiopicus", "", "", "LC", "bird"),
    ("Western Cattle Egret", "Bubulcus ibis", "", "", "LC", "bird"),

    # ---- birds: gamebirds, hornbills, rollers and other bushveld species --------------------------
    ("Helmeted Guineafowl", "Numida meleagris", "Hanga", "Impangele", "LC", "bird"),
    ("Crested Guineafowl", "Guttera pucherani", "", "", "LC", "bird"),
    ("Swainson's Spurfowl", "Pternistis swainsonii", "", "", "LC", "bird"),
    ("Natal Spurfowl", "Pternistis natalensis", "", "", "LC", "bird"),
    ("Crested Francolin", "Ortygornis sephaena", "", "", "LC", "bird"),
    ("African Grey Hornbill", "Lophoceros nasutus", "", "", "LC", "bird"),
    ("Southern Yellow-billed Hornbill", "Tockus leucomelas", "", "", "LC", "bird"),
    ("Southern Red-billed Hornbill", "Tockus rufirostris", "", "", "LC", "bird"),
    ("Trumpeter Hornbill", "Bycanistes bucinator", "", "", "LC", "bird"),
    ("Lilac-breasted Roller", "Coracias caudatus", "", "", "LC", "bird"),
    ("Racket-tailed Roller", "Coracias spatulatus", "", "", "LC", "bird"),
    ("Southern Carmine Bee-eater", "Merops nubicoides", "", "", "LC", "bird"),
    ("White-fronted Bee-eater", "Merops bullockoides", "", "", "LC", "bird"),
    ("Grey Go-away-bird", "Crinifer concolor", "", "", "LC", "bird"),
    ("Purple-crested Turaco", "Gallirex porphyreolophus", "", "", "LC", "bird"),
    ("Livingstone's Turaco", "Tauraco livingstonii", "", "", "LC", "bird"),
    ("Meyer's Parrot", "Poicephalus meyeri", "", "", "LC", "bird"),
    ("Lilian's Lovebird", "Agapornis lilianae", "", "", "NT", "bird"),
    ("African Green Pigeon", "Treron calvus", "", "", "LC", "bird"),
    ("Ring-necked Dove", "Streptopelia capicola", "Njiva", "Ijuba", "LC", "bird"),
    ("Red-billed Oxpecker", "Buphagus erythrorynchus", "", "", "LC", "bird"),
    ("Yellow-billed Oxpecker", "Buphagus africanus", "", "", "LC", "bird"),
    ("Southern Masked Weaver", "Ploceus velatus", "", "", "LC", "bird"),
    ("Red-billed Quelea", "Quelea quelea", "", "", "LC", "bird"),

    # ---- birds: Eastern Highlands specials -------------------------------------------------------
    ("Blue Swallow", "Hirundo atrocaerulea", "", "", "VU", "bird"),
    ("African Pitta", "Pitta angolensis", "", "", "LC", "bird"),
    ("Swynnerton's Robin", "Swynnertonia swynnertoni", "", "", "VU", "bird"),
    ("Chirinda Apalis", "Apalis chirindensis", "", "", "LC", "bird"),
    ("Roberts's Warbler", "Oreophilais robertsi", "", "", "LC", "bird"),
    ("Boulder Chat", "Pinarornis plumosus", "", "", "LC", "bird"),

    # ---- reptiles --------------------------------------------------------------------------------
    ("Nile Crocodile", "Crocodylus niloticus", "Garwe", "Ingwenya", "LC", "reptile"),
    ("Southern African Python", "Python natalensis", "Shato", "Inhlwathi", "LC", "reptile"),
    ("Black Mamba", "Dendroaspis polylepis", "", "", "LC", "reptile"),
    ("Eastern Green Mamba", "Dendroaspis angusticeps", "", "", "LC", "reptile"),
    ("Puff Adder", "Bitis arietans", "", "", "LC", "reptile"),
    ("Rhombic Night Adder", "Causus rhombeatus", "", "", "LC", "reptile"),
    ("Snouted Cobra", "Naja annulifera", "", "", "LC", "reptile"),
    ("Mozambique Spitting Cobra", "Naja mossambica", "", "", "LC", "reptile"),
    ("Boomslang", "Dispholidus typus", "", "", "LC", "reptile"),
    ("Southern Twig Snake", "Thelotornis capensis", "", "", "LC", "reptile"),
    ("Spotted Bush Snake", "Philothamnus semivariegatus", "", "", "LC", "reptile"),
    ("Olive Grass Snake", "Psammophis mossambicus", "", "", "LC", "reptile"),
    ("Brown House Snake", "Boaedon capensis", "", "", "LC", "reptile"),
    ("Rhombic Egg-eater", "Dasypeltis scabra", "", "", "LC", "reptile"),
    ("Common Slug Eater", "Duberria lutrix", "", "", "LC", "reptile"),
    ("Cape File Snake", "Limaformosa capensis", "", "", "LC", "reptile"),
    ("Nile Monitor", "Varanus niloticus", "", "", "LC", "reptile"),
    ("Rock Monitor", "Varanus albigularis", "", "", "LC", "reptile"),
    ("Leopard Tortoise", "Stigmochelys pardalis", "Kamba", "Ufudu", "LC", "reptile"),
    ("Bell's Hinge-back Tortoise", "Kinixys belliana", "", "", "LC", "reptile"),
    ("Speke's Hinge-back Tortoise", "Kinixys spekii", "", "", "LC", "reptile"),
    ("Serrated Hinged Terrapin", "Pelusios sinuatus", "", "", "LC", "reptile"),
    ("Marsh Terrapin", "Pelomedusa subrufa", "", "", "LC", "reptile"),
    ("Flap-necked Chameleon", "Chamaeleo dilepis", "", "", "LC", "reptile"),
    ("Southern Tree Agama", "Acanthocercus atricollis", "", "", "LC", "reptile"),
    ("Common Flat Lizard", "Platysaurus intermedius", "", "", "LC", "reptile"),
    ("Rainbow Skink", "Trachylepis margaritifera", "", "", "LC", "reptile"),
    ("Variable Skink", "Trachylepis varia", "", "", "LC", "reptile"),
    ("Tropical House Gecko", "Hemidactylus mabouia", "", "", "LC", "reptile"),
    ("Turner's Thick-toed Gecko", "Chondrodactylus turneri", "", "", "LC", "reptile"),

    # ---- amphibians ------------------------------------------------------------------------------
    ("Giant Bullfrog", "Pyxicephalus adspersus", "", "", "LC", "amphibian"),
    ("Guttural Toad", "Sclerophrys gutturalis", "", "", "LC", "amphibian"),
    ("Southern Foam-nest Frog", "Chiromantis xerampelina", "", "", "LC", "amphibian"),
    ("Painted Reed Frog", "Hyperolius marmoratus", "", "", "LC", "amphibian"),
    ("Bubbling Kassina", "Kassina senegalensis", "", "", "LC", "amphibian"),
    ("Cave Squeaker", "Arthroleptis troglodytes", "", "", "CR", "amphibian"),

    # ---- fish ------------------------------------------------------------------------------------
    ("Tigerfish", "Hydrocynus vittatus", "", "", "LC", "fish"),
    ("Kariba Tilapia", "Oreochromis mortimeri", "", "", "CR", "fish"),
    ("Nile Tilapia", "Oreochromis niloticus", "", "", "LC", "fish"),
    ("Sharptooth Catfish", "Clarias gariepinus", "", "", "LC", "fish"),
    ("Vundu", "Heterobranchus longifilis", "", "", "LC", "fish"),
    ("Cornish Jack", "Mormyrops anguilloides", "", "", "LC", "fish"),

    # ---- invertebrates ---------------------------------------------------------------------------
    ("Mopane Worm Emperor Moth", "Gonimbrasia belina", "Madora", "Amacimbi", "NE", "invertebrate"),
    ("Western Honey Bee", "Apis mellifera", "", "", "DD", "invertebrate"),
    ("Giant African Millipede", "Archispirostreptus gigas", "", "", "NE", "invertebrate"),
    ("Matabele Ant", "Megaponera analis", "", "", "NE", "invertebrate"),
    ("Citrus Swallowtail", "Papilio demodocus", "", "", "NE", "invertebrate"),
]

TAXON_GROUPS = ["mammal", "bird", "reptile", "amphibian", "fish", "invertebrate"]
