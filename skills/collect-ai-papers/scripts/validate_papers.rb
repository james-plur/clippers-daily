#!/usr/bin/env ruby
# frozen_string_literal: true

require "yaml"

path = ARGV.fetch(0, "config/papers.yaml")
data = YAML.load_file(path)
errors = []

unless data.is_a?(Hash) && data["sources"].is_a?(Array)
  abort "ERROR: sources must be an array"
end

sources = data["sources"]
source_ids = sources.map { |source| source["id"] }
duplicates = source_ids.compact.group_by(&:itself).select { |_id, values| values.length > 1 }.keys
errors << "duplicate source ids: #{duplicates.join(', ')}" unless duplicates.empty?

required_source = %w[id name enabled priority role adapter schedule]
sources.each_with_index do |source, index|
  label = source["id"] || "index #{index}"
  missing = required_source.reject { |key| source.key?(key) }
  errors << "#{label}: missing #{missing.join(', ')}" unless missing.empty?
  errors << "#{label}: enabled must be boolean" unless [true, false].include?(source["enabled"])
end

paperlab = sources.find { |source| source["id"] == "paperlab" }
if paperlab
  conferences = paperlab["conferences"]
  errors << "paperlab: conferences must be an array" unless conferences.is_a?(Array)
  if conferences.is_a?(Array)
    conference_ids = conferences.map { |conference| conference["id"] }
    duplicate_conferences = conference_ids.compact.group_by(&:itself).select { |_id, values| values.length > 1 }.keys
    errors << "paperlab: duplicate conference ids #{duplicate_conferences.join(', ')}" unless duplicate_conferences.empty?
    conferences.each do |conference|
      label = "paperlab/#{conference['id'] || 'unknown'}"
      %w[id name priority mode].each do |key|
        errors << "#{label}: missing #{key}" unless conference.key?(key)
      end
      if conference.key?("enabled") && ![true, false].include?(conference["enabled"])
        errors << "#{label}: enabled must be boolean when present"
      end
    end
  end
end

hf = sources.find { |source| source["id"] == "huggingface-daily-papers" }
if hf
  errors << "huggingface-daily-papers: missing endpoint" unless hf["endpoint"]
  errors << "huggingface-daily-papers: selection must be a map" unless hf["selection"].is_a?(Hash)
end

unless errors.empty?
  warn errors.map { |error| "ERROR: #{error}" }.join("\n")
  exit 1
end

conference_count = paperlab ? paperlab.fetch("conferences", []).length : 0
enabled_sources = sources.select { |source| source["enabled"] }.map { |source| source["id"] }
puts "OK: #{sources.length} sources, #{conference_count} conferences"
puts "Enabled sources: #{enabled_sources.empty? ? 'none' : enabled_sources.join(', ')}"

