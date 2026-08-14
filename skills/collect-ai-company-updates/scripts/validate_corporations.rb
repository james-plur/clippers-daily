#!/usr/bin/env ruby
# frozen_string_literal: true

require "yaml"

path = ARGV.fetch(0, "config/coporations.yaml")
data = YAML.load_file(path)
errors = []

unless data.is_a?(Hash) && data["corporations"].is_a?(Array)
  abort "ERROR: corporations must be an array"
end

corporations = data["corporations"]
corporation_ids = corporations.map { |item| item["id"] }
duplicates = corporation_ids.compact.group_by(&:itself).select { |_id, values| values.length > 1 }.keys
errors << "duplicate corporation ids: #{duplicates.join(', ')}" unless duplicates.empty?

required_corporation = %w[id name enabled priority focus channels]
required_channel = %w[id name priority type endpoint schedule]
enabled = []

corporations.each_with_index do |corporation, corporation_index|
  label = corporation["id"] || "index #{corporation_index}"
  missing = required_corporation.reject { |key| corporation.key?(key) }
  errors << "#{label}: missing #{missing.join(', ')}" unless missing.empty?
  errors << "#{label}: enabled must be boolean" unless [true, false].include?(corporation["enabled"])
  next unless corporation["channels"].is_a?(Array)

  channel_ids = corporation["channels"].map { |channel| channel["id"] }
  channel_duplicates = channel_ids.compact.group_by(&:itself).select { |_id, values| values.length > 1 }.keys
  errors << "#{label}: duplicate channel ids #{channel_duplicates.join(', ')}" unless channel_duplicates.empty?

  corporation["channels"].each_with_index do |channel, channel_index|
    channel_label = "#{label}/#{channel['id'] || channel_index}"
    channel_missing = required_channel.reject { |key| channel.key?(key) }
    errors << "#{channel_label}: missing #{channel_missing.join(', ')}" unless channel_missing.empty?
    if channel.key?("enabled") && ![true, false].include?(channel["enabled"])
      errors << "#{channel_label}: enabled must be boolean when present"
    end
    channel_enabled = channel.fetch("enabled", true)
    enabled << channel_label if corporation["enabled"] && channel_enabled
  end
end

unless errors.empty?
  warn errors.map { |error| "ERROR: #{error}" }.join("\n")
  exit 1
end

channel_count = corporations.sum { |corporation| corporation.fetch("channels", []).length }
puts "OK: #{corporations.length} corporations, #{channel_count} channels"
puts "Enabled channels: #{enabled.empty? ? 'none' : enabled.join(', ')}"
