#!/usr/bin/env ruby
# frozen_string_literal: true

require "yaml"

path = ARGV.fetch(0, "config/media.yaml")
data = YAML.load_file(path)
errors = []

unless data.is_a?(Hash) && data["sources"].is_a?(Array)
  abort "ERROR: sources must be an array"
end

sources = data["sources"]
ids = sources.map { |source| source["id"] }
duplicates = ids.compact.group_by(&:itself).select { |_id, values| values.length > 1 }.keys
errors << "duplicate source ids: #{duplicates.join(', ')}" unless duplicates.empty?

required = %w[id name language category enabled priority channels]
sources.each_with_index do |source, index|
  label = source["id"] || "index #{index}"
  missing = required.reject { |key| source.key?(key) }
  errors << "#{label}: missing #{missing.join(', ')}" unless missing.empty?
  errors << "#{label}: enabled must be boolean" unless [true, false].include?(source["enabled"])

  channels = source["channels"]
  unless channels.is_a?(Array) && !channels.empty?
    errors << "#{label}: channels must be a non-empty array"
    next
  end

  channel_ids = channels.map { |channel| channel["id"] }
  duplicate_channels = channel_ids.compact.group_by(&:itself).select { |_id, values| values.length > 1 }.keys
  errors << "#{label}: duplicate channel ids #{duplicate_channels.join(', ')}" unless duplicate_channels.empty?
  channels.each do |channel|
    channel_label = "#{label}/#{channel['id'] || 'unknown'}"
    %w[id adapter status].each do |key|
      errors << "#{channel_label}: missing #{key}" unless channel.key?(key)
    end
    if channel.key?("enabled") && ![true, false].include?(channel["enabled"])
      errors << "#{channel_label}: enabled must be boolean when present"
    end
  end
end

unless errors.empty?
  warn errors.map { |error| "ERROR: #{error}" }.join("\n")
  exit 1
end

enabled = sources.select { |source| source["enabled"] }
enabled_channels = enabled.sum do |source|
  source.fetch("channels", []).count { |channel| channel.fetch("enabled", true) }
end
puts "OK: #{sources.length} sources, #{enabled.length} enabled, #{enabled_channels} enabled channels"
puts "Enabled sources: #{enabled.map { |source| source['id'] }.join(', ')}"
