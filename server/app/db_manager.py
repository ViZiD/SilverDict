"""
A custom database manager.
I do not use SQLAlchemy because I have not figured out how I can create and drop indexes as I need.
After all, it is efficient, thread-safe, and easy to use.
"""
"""
Performance note:
dictionary_exists(): very good with idx_dictname
get_entries(): very good with idx_key_dictname_word
select_entries_like(): I don't care, GoldenDict is also quite slow in this respect
entry_exists_in_dictionary(), entry_exists_in_dictionaries(): very good with idx_key_dictname_word

If idx_dictname exists, select_entries_beginning_with() would use it instead of idx_key_dictname_word(), which slows things down. Anyway dictionary_exists() is only used when initialising a dictionary reader, so it is not a big deal.

---

What really matters is select_entries_beginning_with():

Should record the highest UTF-8 character in the dictionary and use it to perform optimisation by hand:
select * from entries where key like "key%" => select * from entries where key >= "key" and key < "keyzzzzzzz";

But now, experimentally, I am using a lazy approach: just key < "key𱍊" (U+3134A, decimal 201546)

'Contains' search is now implemented with ngrams by J.F. Dockes. The performance is staggering.
"""

import sqlite3
import threading
from .settings import Settings

local_storage = threading.local()

# n-gram related helpers
def _gen_ngrams(input: str, ngramlen: int) -> list[str]:
	ngrams = []
	if len(input) >= ngramlen:
		for i in range(len(input) - ngramlen + 1):
			ngrams.append(input[i:i + ngramlen])
	return ngrams


def get_connection() -> sqlite3.Connection:
	if not hasattr(local_storage, 'connection'):
		local_storage.connection = sqlite3.connect(Settings.SQLITE_DB_FILE)
	return local_storage.connection


def get_cursor() -> sqlite3.Cursor:
	if not hasattr(local_storage, 'cursor'):
		local_storage.cursor = get_connection().cursor()
	return local_storage.cursor


def create_table_entries() -> None:
	cursor = get_cursor()
	cursor.execute('''create table if not exists entries (
		key text, -- the entry in lowercase and without accents
		dictionary_name text, -- identifying name of the dictionary
		word text, -- the entry as it appears in the dictionary
		offset integer, -- offset of the entry in the dictionary file
		size integer -- size of the definition in bytes
	)''')


def dictionary_exists(dictionary_name: str) -> bool:
	cursor = get_cursor()
	# cursor.execute('select count(*) from entries where dictionary_name = ?', (dictionary_name,))
	# return cursor.fetchone()[0] > 0
	cursor.execute('select key from entries where dictionary_name = ? limit 1', (dictionary_name,))
	return cursor.fetchone() is not None  # This is faster


def add_entry(key: str,
			  dictionary_name: str,
			  word: str,
			  offset: int,
			  size: int) -> None:
	"Commit manually!"
	cursor = get_cursor()
	cursor.execute('insert into entries values (?, ?, ?, ?, ?)', (key, dictionary_name, word, offset, size))


def commit() -> None:
	get_connection().commit()


def create_ngram_table(stores_keys: bool) -> None:
	cursor = get_cursor()
	cursor.execute('drop index if exists ngrams_ngram')
	cursor.execute('drop table if exists ngrams')
	cursor.execute('create table ngrams (ngram text, idxs text)')
	cursor.execute('create index ngrams_ngram on ngrams (ngram)')

	if stores_keys:
		rows = cursor.execute('select distinct key from entries')

		# Get another cursor for the ngrams table
		c1 = get_connection().cursor()
		for row in rows:
			key = row[0]
			if len(key) >= Settings.NGRAM_LEN:
				ngrams = _gen_ngrams(key, Settings.NGRAM_LEN)
				for ngram in ngrams:
					c1.execute('insert into ngrams (ngram, idxs) values (?, ?)', (ngram, key))
	else:
		# Walk the whole entries table. For each row, generate ngrams from the key, and either create a row
		# in the ngrams table, with the ngram and rowid, or append the rowid to an existing ngram record
		# This is relatively slow (maybe 1/2 hour for a 5 million entries dict on a laptop).
		rows = cursor.execute('select key, rowid from entries')

		# Get another cursor for the ngrams table
		c1 = get_connection().cursor()
		for row in rows:
			key = row[0]
			rowid = str(row[1])
			if len(key) >= Settings.NGRAM_LEN:
				ngrams = _gen_ngrams(key, Settings.NGRAM_LEN)
				for ngram in ngrams:
					c1.execute('select idxs from ngrams where ngram = ?', (ngram,))
					row = c1.fetchone()
					if row:
						idxs = row[0]
						idxs += "," + rowid
						c1.execute('update ngrams set idxs = ? where ngram = ?', (idxs, ngram))
					else:
						c1.execute('insert into ngrams (idxs, ngram) values (?, ?)', (rowid, ngram))

	get_connection().commit()


def get_entries(key: str, dictionary_name: str) -> list[tuple[str, int, int]]:
	"""
	Returns a list of (word, offset, size).
	"""
	cursor = get_cursor()
	cursor.execute('select word, offset, size from entries where key = ? and dictionary_name = ?',
				   (key, dictionary_name))
	return cursor.fetchall()


def headword_count_of_dictionary(dictionary_name: str) -> int:
	cursor = get_cursor()
	cursor.execute('select count(*) from entries where dictionary_name = ?', (dictionary_name,))
	return cursor.fetchone()[0]


def get_entries_with_headword(word: str, dictionary_name: str) -> list[tuple[int, int]]:
	"""
	Returns a list of (offset, size)
	"""
	cursor = get_cursor()
	cursor.execute('select offset, size from entries where word = ? and dictionary_name = ?',
				   (word, dictionary_name))
	return cursor.fetchall()


def get_entries_all(dictionary_name: str) -> list[tuple[str, str, int, int]]:
	"""
	Returns a list of (key, word, offset, size).
	"""
	cursor = get_cursor()
	cursor.execute('select key, word, offset, size from entries where dictionary_name = ? order by offset',
				   (dictionary_name,))
	return cursor.fetchall()


def delete_dictionary(dictionary_name: str) -> None:
	cursor = get_cursor()
	cursor.execute('delete from entries where dictionary_name = ?', (dictionary_name,))
	get_connection().commit()


def create_index() -> None:
	cursor = get_cursor()
	# cursor.execute('create index idx_dictname on entries (dictionary_name)') # This helps with dictionary_exists()
	cursor.execute('create index idx_key_dictname on entries (key, dictionary_name)')
	# cursor.execute('create index idx_key on entries (key)')
	# cursor.execute('create index idx_key_dictname_word on entries (key, dictionary_name, word)')
	cursor.execute('create index idx_word_dictname on entries (word, dictionary_name)')


def drop_index() -> None:
	cursor = get_cursor()
	# For backwards compatibility
	cursor.execute('drop index if exists idx_dictname')
	cursor.execute('drop index if exists idx_key_dictname_word')
	cursor.execute('drop index if exists idx_key')
	####
	cursor.execute('drop index if exists idx_key_dictname')
	cursor.execute('drop index if exists idx_word_dictname')


def select_words_of_dictionary(dictionary_name: str) -> list[str]:
	cursor = get_cursor()
	cursor.execute('select distinct word from entries where dictionary_name = ?', (dictionary_name,))
	return [row[0] for row in cursor.fetchall()]


def select_entries_beginning_with(keys: list[str],
								  names_dictionaries: list[str],
								  words_already_found: list[str],
								  limit: int) -> list[str]:
	"""
	Return the first ten entries (word) in the dictionaries that begin with the given keys.
	"""
	limit -= len(words_already_found)
	cursor = get_cursor()
	result = []
	for key in keys:
		cursor.execute(
			f'''select distinct word from entries
				where key >= ? and key < ?
				and dictionary_name in ({','.join('?' * len(names_dictionaries))})
				and word not in ({','.join('?' * len(words_already_found))})
				order by key
				limit ?''',
			(key, key + '\U0003134A', *names_dictionaries, *words_already_found, limit))
		result.extend([row[0] for row in cursor.fetchall()])
		limit = limit - len(result)
		if limit <= 0:
			break
	return result


def select_entries_containing(key: str,
							  names_dictionaries: list[str],
							  words_already_found: list[str],
							  limit: int) -> list[str]:
	"""
	Return the first num_suggestions - len(words_already_found) entries (word)
	in the dictionaries that contain key.
	"""
	num_words = limit - len(words_already_found)
	cursor = get_cursor()
	cursor.execute(
		f'''select distinct word from entries
			where key like ?
			and dictionary_name in ({','.join('?' * len(names_dictionaries))})
			and word not in ({','.join('?' * len(words_already_found))})
			order by key
			limit ?''',
		(f'%{key}%', *names_dictionaries, *words_already_found, num_words))
	return [row[0] for row in cursor.fetchall()]


def expand_key(input: str, stores_keys: bool) -> list[str]:
	ngrams = _gen_ngrams(input, Settings.NGRAM_LEN)
	if len(ngrams) == 0:
		return []

	cursor = get_cursor()
	statement = f'select idxs from ngrams where ngram in ({",".join("?" * len(ngrams))})'
	rows = cursor.execute(statement, ngrams)

	if stores_keys:
		selected_keys = list(set((row[0] for row in rows)))
		selected_keys = [key for key in selected_keys if key.find(input) != -1]
		if len(selected_keys) > Settings.SQLITE_LIMIT_VARIABLE_NUMBER:
			selected_keys = selected_keys[:Settings.SQLITE_LIMIT_VARIABLE_NUMBER]
	else:
		# Intersect the lists yielded by the different ngrams
		selected_idxs = None
		for row in rows:
			if selected_idxs is None:
				selected_idxs = set(row[0].split(','))
			else:
				selected_idxs = selected_idxs & set(row[0].split(','))

		if not selected_idxs:
			return []

		if len(selected_idxs) > Settings.SQLITE_LIMIT_VARIABLE_NUMBER:
			selected_idxs = list(selected_idxs)[:Settings.SQLITE_LIMIT_VARIABLE_NUMBER]
		# Get the keys corresponding to the selected rowids
		statement = f'select key from entries where rowid in ({",".join("?" * len(selected_idxs))})'
		rows = cursor.execute(statement, [int(idx) for idx in selected_idxs])
		selected_keys = [row[0] for row in rows]

		# Only select the keys where the input is found (ngrams contiguous in the right order)
		# Actually this usually filters nothing
		selected_keys = [key for key in selected_keys if key.find(input) != -1]

	return selected_keys


def select_entries_with_keys(keys: list[str],
							 names_dictionaries: list[str],
							 words_already_found: list[str],
							 limit: int) -> list[str]:
	num_words = limit - len(words_already_found)
	cursor = get_cursor()
	cursor.execute(
		f'''select distinct word from entries
			where key in ({','.join('?' * len(keys))})
			and dictionary_name in ({','.join('?' * len(names_dictionaries))})
			and word not in ({','.join('?' * len(words_already_found))})
			order by key
			limit ?''',
		(*keys, *names_dictionaries, *words_already_found, num_words))
	return [row[0] for row in cursor.fetchall()]


def select_entries_like(key: str, names_dictionaries: list[str], limit: int) -> list[str]:
	"""
	Return the first ten entries matched.
	"""
	cursor = get_cursor()
	cursor.execute(
		f'''select distinct word from entries
			where key like ?
			and dictionary_name in ({','.join('?' * len(names_dictionaries))})
			order by key
			limit ?''',
		(key, *names_dictionaries, limit))
	return [row[0] for row in cursor.fetchall()]


def entry_exists_in_dictionary(key: str, dictionary_name: str) -> bool:
	cursor = get_cursor()
	# cursor.execute('select count(*) from entries where key = ? and dictionary_name = ?', (key, dictionary_name))
	# return cursor.fetchone()[0] > 0
	cursor.execute('select key from entries where key = ? and dictionary_name = ? limit 1',
				   (key, dictionary_name))
	return cursor.fetchone() is not None


def headword_exists_in_dictionary(word: str, dictionary_name: str) -> bool:
	cursor = get_cursor()
	cursor.execute('select word from entries where word = ? and dictionary_name = ? limit 1',
				   (word, dictionary_name))
	return cursor.fetchone() is not None


def entry_exists_in_dictionaries(key: str, names_dictionaries: list[str]) -> bool:
	cursor = get_cursor()
	# cursor.execute('select count(*) from entries where key = ? and dictionary_name in (%s)' % ','.join('?' * len(names_dictionaries)), (key, *names_dictionaries))
	# return cursor.fetchone()[0] > 0
	cursor.execute(
		f'''select key from entries
			where key = ?
			and dictionary_name in ({','.join('?' * len(names_dictionaries))})
			limit 1''',
		(key, *names_dictionaries))
	return cursor.fetchone() is not None
