from flask import Flask, send_from_directory, make_response, jsonify, request
import json
import os
import shutil
from .config import Config
from . import db_manager
from .dicts.base_reader import BaseReader
from .dicts.mdict_reader import MDictReader
from .dicts.stardict_reader import StarDictReader
from .dicts.dsl_reader import DSLReader
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class SilverDict(Flask):
	def _load_dictionary(self, dictionary_info: 'dict') -> 'None':
		match dictionary_info['dictionary_format']:
			case 'MDict (.mdx)':
				self.dictionaries[dictionary_info['dictionary_name']] = MDictReader(dictionary_info['dictionary_name'], dictionary_info['dictionary_filename'], dictionary_info['dictionary_display_name'], db_manager.dictionary_exists, db_manager.add_entry, db_manager.commit, db_manager.get_entries, db_manager.create_index, db_manager.drop_index)
			case 'StarDict (.ifo)':
				self.dictionaries[dictionary_info['dictionary_name']] = StarDictReader(dictionary_info['dictionary_name'], dictionary_info['dictionary_filename'], dictionary_info['dictionary_display_name'], db_manager.dictionary_exists, db_manager.add_entry, db_manager.commit, db_manager.get_entries, db_manager.create_index, db_manager.drop_index)
			case 'DSL (.dsl/.dsl.dz)':
				self.dictionaries[dictionary_info['dictionary_name']] = DSLReader(dictionary_info['dictionary_name'], dictionary_info['dictionary_filename'], dictionary_info['dictionary_display_name'], db_manager.dictionary_exists, db_manager.add_entry, db_manager.commit, db_manager.get_entries, db_manager.create_index, db_manager.drop_index)	
			case _:
				raise ValueError('Dictionary format %s not supported' % dictionary_info['dictionary_format'])

	def _load_dictionaries(self) -> 'None':
		for dictionary_info in self.configs.dictionary_list:
			self._load_dictionary(dictionary_info)

	def __init__(self) -> 'None':
		super().__init__(__name__)
		self.configs = Config()
		db_manager.create_table_entries()

		# Load the dictionaries
		self.dictionaries : 'dict[str, BaseReader]' = dict()
		self._load_dictionaries()

		# Define routes
		# Define cache (dictionary resources) directory:
		@self.route('/api/cache/<path:path_name>')
		def send_cached_resources(path_name: 'str'):
			response = send_from_directory(Config.CACHE_ROOT, path_name)
			return response

		# Define lookup API:
		@self.route('/api/lookup/<dictionary_name>/<entry>')
		def lookup(dictionary_name: 'str', entry: 'str'):
			if not dictionary_name in self.dictionaries.keys():
				response = make_response('<p>Dictionary %s not found</p>' % dictionary_name)
				response.status_code = 404
			elif not db_manager.entry_exists_in_dictionary(entry, dictionary_name):
				response = make_response('<p>Entry %s not found in dictionary %s</p>' % (entry, dictionary_name))
				response.status_code = 404
			else:
				response = make_response(self.dictionaries[dictionary_name].entry_definition(entry))
				self.configs.add_word_to_history(entry)
			return response
		
		# Define dictionary metadata RESTful API's:
		# Get dictionary list, add, delete, update
		@self.route('/api/metadata/dictionary_list', methods=['GET', 'POST', 'DELETE', 'PUT'])
		def dictionary_list():
			if request.method == 'GET':
				response = jsonify(self.configs.dictionary_list)
			elif request.method == 'POST':
				dictionary_info = request.get_json()
				
				# Check for duplicates
				# if dictionary_info in self.configs.dictionary_list:
				# 	raise ValueError('Dictionary %s already present' % dictionary_info)
				# TODO: well, the exception won't be caught by the client anyway, and maybe we should allow duplicates?
			
				self.configs.dictionary_list.append(dictionary_info)
				self._load_dictionary(dictionary_info)

				self.configs.save_dictionary_list()

				response = jsonify(self.configs.dictionary_list)
			elif request.method == 'DELETE':
				dictionary_info = request.get_json()

				# # Check if the dictionary is in the list
				# if not dictionary_info in self.configs.dictionary_list:
				# 	raise ValueError('Dictionary %s not in the list' % dictionary_info)
				# TODO: I think this simply won't happen, and, anyway, the exception won't be caught by the client

				self.configs.dictionary_list.remove(dictionary_info)
				del self.dictionaries[dictionary_info['dictionary_name']]
				resources_dir = os.path.join(Config.CACHE_ROOT, dictionary_info['dictionary_name'])
				if os.path.isdir(resources_dir):
					shutil.rmtree(resources_dir)

				self.configs.save_dictionary_list()

				db_manager.delete_dictionary(dictionary_info['dictionary_name'])

				logger.info('Dictionary %s deleted' % dictionary_info['dictionary_name'])

				response = jsonify(self.configs.dictionary_list)
			elif request.method == 'PUT':
				self.configs.dictionary_list = request.get_json()

				self._load_dictionaries()

				self.configs.save_dictionary_list()

				response = jsonify(self.configs.dictionary_list)
			else:
				raise ValueError('Invalid request method %s' % request.method)
			return response

		# Define dictionary entry list lookup API (return the first ten entries that contain `key`)
		@self.route('/api/metadata/entry_list/<dictionary_name>/<key>')
		def entry_list(dictionary_name: 'str', key: 'str'):
			if not dictionary_name in self.dictionaries.keys():
				response = make_response('<p>Dictionary %s not found</p>' % dictionary_name)
				response.status_code = 404
			else:
				key = BaseReader.simplify(key)
				if any(wildcard in key for wildcard in self.configs.WILDCARDS.keys()):
					# Replace custom wildcards with SQL wildcards
					for wildcard, sql_wildcard in self.configs.WILDCARDS.items():
						key = key.replace(wildcard, sql_wildcard)
					candidates = db_manager.select_entries_like(key, dictionary_name)
				else:
					# First search for entries beginning with `key`, as is common sense
					candidates_beginning_with_key = db_manager.select_entries_beginning_with(key, dictionary_name)
					# Then it's just 'contains' searching
					candidates_containing_key = db_manager.select_entries_containing(key, dictionary_name, candidates_beginning_with_key)
					# Fill the list with blanks if there are less than 10 candidates
					candidates = candidates_beginning_with_key + candidates_containing_key
				while len(candidates) < 10:
					candidates.append('')
				response = jsonify(candidates)
			return response

		# Define lookup history APIs
		@self.route('/api/metadata/history', methods=['GET', 'PUT'])
		def history():
			if request.method == 'GET':
				response = jsonify(self.configs.lookup_history)
			elif request.method == 'PUT':
				self.configs.lookup_history = request.get_json()
				self.configs.save_history()
				response = jsonify(self.configs.lookup_history)
			else:
				raise ValueError('Invalid request method %s' % request.method)
			return response
		
		@self.route('/api/metadata/history_size', methods=['GET', 'PUT'])
		def history_size():
			if request.method == 'GET':
				response = jsonify({
					"history_size": int(self.configs.misc_configs['history_size'])
				})
			elif request.method == 'PUT':
				self.configs.misc_configs['history_size'] = int(request.get_json()['history_size'])
				self.configs.save_misc_configs()
				response = jsonify({
					"history_size": int(self.configs.misc_configs['history_size'])
				})
			else:
				raise ValueError('Invalid request method %s' % request.method)
			return response
		
		# Define API for getting supported dictionary formats
		@self.route('/api/metadata/supported_dictionary_formats')
		def supported_dictionary_formats():
			response = jsonify(list(self.configs.SUPPORTED_DICTIONARY_FORMATS.keys()))
			return response
		
		# Define a separate set of validation APIs
		@self.route('/api/metadata/validator/dictionary_info', methods=['POST'])
		def dictionary_info_validator():
			dictionary_info = request.get_json()
			response = jsonify({
				"valid": self.configs.dictionary_info_valid(dictionary_info)
			})
			return response

