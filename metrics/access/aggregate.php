#!/usr/bin/php
<?php

use InfluxDB\Point;
use InfluxDB\Database;

$CACHE_DIR = $_SERVER['HOME'] . '/.cache/openSUSE-release-tools/metrics-access';
const PROTOCOLS = ['ipv4', 'ipv6'];
const PONTIFEX = 'http://pontifex.infra.opensuse.org/logs';
const LANGLEY = 'http://langley.suse.de/pub/pontifex%s-opensuse.suse.de';
const VHOST = 'download.opensuse.org';
const FILENAME = 'download.opensuse.org-%s-access_log.xz';
const IPV6_PREFIX = 'ipv6.';
const PRODUCT_PATTERN = '/^(10\.[2-3]|11\.[0-4]|12\.[1-3]|13\.[1-2]|42\.[1-3]|15\.[0-4]|tumbleweed)$/';

$begin = new DateTime();
// Skip the current day since the logs are incomplete and not compressed yet.
$begin->sub(date_interval_create_from_date_string('1 day'));
$source_map = [
  'ipv4' => [
    '2010-01-03' => false,
    '2014-04-14' => sprintf(LANGLEY, 2) . '/' . VHOST,
    '2017-12-04' => sprintf(LANGLEY, 3) . '/' . VHOST,
    // 2017-12-05 has bad permissions on langley and is still on origin.
    '2021-09-05' => false,
    $begin->format('Y-m-d') => PONTIFEX . '/' . VHOST,
    'filename' => FILENAME,
  ],
  'ipv6' => [
    '2012-12-31' => false,
    '2017-12-04' => sprintf(LANGLEY, 3) . '/' . IPV6_PREFIX . VHOST,
    '2021-09-05' => false,
    $begin->format('Y-m-d') => PONTIFEX . '/' . IPV6_PREFIX . VHOST,
    'filename' => IPV6_PREFIX . FILENAME,
  ],
];
$end = new DateTime(key($source_map['ipv4'])); // decide about adding one day
$period_reversed = date_period_reversed($end, '1 day', $begin);

error_log('begin: ' . $begin->format('Y-m-d'));
error_log('end:   ' . $end->format('Y-m-d'));
error_log('count: ' . number_format(count($period_reversed)) . ' days');

cache_init();
ingest_all($period_reversed, $source_map);
aggregate_all(array_reverse($period_reversed));


function cache_init()
{
  global $CACHE_DIR;
  if (!file_exists($CACHE_DIR)) {
    foreach (PROTOCOLS as $protocol) {
      mkdir("$CACHE_DIR/$protocol", 0755, true);
    }

    // Avoid packaging mess while still automating, but not ideal.
    passthru('cd ' . escapeshellarg($CACHE_DIR) . ' && composer require influxdb/influxdb-php ~1');
  }

  require "$CACHE_DIR/vendor/autoload.php";
}

function ingest_all($period_reversed, $source_map)
{
  global $CACHE_DIR;
  $source = [];
  $found = [];
  // Walk backwards until found in cache.
  foreach ($period_reversed as $date) {
    $date_string = $date->format('Y-m-d');

    foreach (PROTOCOLS as $protocol) {
      if (!empty($found[$protocol])) continue;
      if (isset($source_map[$protocol][$date_string]))
        $source[$protocol] = $source_map[$protocol][$date_string];

      // Skip date+protocol if no source is available.
      if (empty($source[$protocol])) continue;

      $cache_file = "$CACHE_DIR/$protocol/$date_string.json";
      if (file_exists($cache_file)) {
        error_log("[$date_string] [$protocol] found");
        $found[$protocol] = true;
      } else {
        error_log("[$date_string] [$protocol] ingest");
        ingest($date, $source[$protocol], $source_map[$protocol]['filename'], $cache_file);
      }
    }

    if (count($found) == count(PROTOCOLS)) {
      error_log('ingest initialization complete');
      break;
    }
  }

  // Wait for all ingest processes to complete before proceeding.
  subprocess_wait(1, 1);
}

function ingest($date, $source, $filename, $destination)
{
  $url = implode('/', [
    $source,
    $date->format('Y'),
    $date->format('m'),
    sprintf($filename, $date->format('Ymd')),
  ]);
  $command = implode(' ', [
    'curl -s',
    escapeshellarg($url),
    '| xzcat',
    '| ' . __DIR__ . '/ingest.php',
    '> ' . escapeshellarg($destination),
    '&',
  ]);
  error_log($command);
  passthru_block($command);
}

function passthru_block($command)
{
  static $cpu_count = null;

  if (!$cpu_count) {
    $cpuinfo = file_get_contents('/proc/cpuinfo');
    preg_match_all('/^processor/m', $cpuinfo, $matches);
    $cpu_count = max(count($matches[0]), 1);
    error_log("detected $cpu_count cores");
  }

  $group_size = substr_count($command, '|') + 1;
  subprocess_wait($group_size, $cpu_count);

  passthru($command, $exit_code);
  if ($exit_code != 0) {
    error_log('failed to start process');
    exit(1);
  }
}

function subprocess_wait($group_size, $cap)
{
  while (subprocess_count() / $group_size >= $cap) {
    usleep(250000);
  }
}

function subprocess_count()
{
  return substr_count(shell_exec('pgrep -g ' . getmypid()), "\n") - 1;
}

function aggregate_all($period)
{
  global $CACHE_DIR;
  $intervals = ['day' => 'Y-m-d', 'week' => 'Y-W', 'month' => 'Y-m', 'FQ' => null, 'FY' => null];
  $merged = [];
  $merged_protocol = [];
  $date_previous = null;
  foreach ($period as $date) {
    $date_string = $date->format('Y-m-d');

    $data = null;
    foreach (PROTOCOLS as $protocol) {
      $cache_file = "$CACHE_DIR/$protocol/$date_string.json";
      if (!file_exists($cache_file) or !filesize($cache_file)) continue;

      error_log("[$date_string] [$protocol] load cache");
      $data_new = json_decode(file_get_contents($cache_file), true);
      if (!$data_new) {
        error_log('ERROR: failed to load ' . $cache_file);
        unlink($cache_file); // Trigger it to be re-ingested next run.
        exit(1);
      }

      if (!isset($merged_protocol[$protocol])) $merged_protocol[$protocol] = [];
      $data_new['days'] = 1;
      normalize($data_new);
      aggregate($intervals, $merged_protocol[$protocol], $date, $date_previous, $data_new,
        ['protocol' => $protocol], 'protocol');

      if ($data) {
        merge($data, $data_new);
        $data['days'] = 1;
      } else {
        $data = $data_new;
      }
    }

    if (!$data) {
      error_log("[$date_string] skipping due to lack of data");
      continue;
    }

    aggregate($intervals, $merged, $date, $date_previous, $data);

    $date_previous = $date;
  }

  // Write out any remaining data by simulating a date beyond all intervals.
  error_log('write remaining data');
  $date = clone $date;
  $date->add(date_interval_create_from_date_string('1 year'));

  foreach (PROTOCOLS as $protocol) {
    aggregate($intervals, $merged_protocol[$protocol], $date, $date_previous, null,
      ['protocol' => $protocol], 'protocol');
  }
  aggregate($intervals, $merged, $date, $date_previous, null);
}

function aggregate($intervals, &$merged, $date, $date_previous, $data, $tags = [], $prefix = 'access')
{
  foreach ($intervals as $interval => $format) {
    if ($interval == 'FQ') {
      $value = format_FQ($date);
      if (isset($date_previous))
        $value_previous = format_FQ($date_previous);
    }
    elseif ($interval == 'FY') {
      $value = format_FY($date);
      if (isset($date_previous))
        $value_previous = format_FY($date_previous);
    }
    else {
      $value = $date->format($format);
      if (isset($date_previous))
        $value_previous = $date_previous->format($format);
    }
    if (!isset($merged[$interval]) || $value != $merged[$interval]['value']) {
      if (!empty($merged[$interval]['data'])) {
        $summary = summarize($merged[$interval]['data']);
        if ($prefix == 'protocol') {
          $summary = ['-' => $summary['-']];
        }

        if (isset($value_previous) and $value != $value_previous) {
          $count = write_summary($interval, $date_previous, $summary, $tags, $prefix);

          if ($prefix == 'access') {
            $summary = summarize_product_plus_key($merged[$interval]['data']['total_image_product']);
            $count += write_summary_product_plus_key($interval, $date_previous, $summary, 'image');
          }

          error_log("[$prefix] [$interval] [{$merged[$interval]['value']}] wrote $count points at " .
            $date_previous->format('Y-m-d') . " spanning " . $merged[$interval]['data']['days'] . ' day(s)');
        }
      }

      // Reset merge data to current data.
      $merged[$interval] = [
        'data' => $data,
        'value' => $value,
      ];
    }
    // Merge day onto existing data for interval. A more complex approach of
    // merging higher order intervals is overly complex due to weeks.
    else
      merge($merged[$interval]['data'], $data);
  }
}

function format_FQ($date)
{
  $financial_date = clone $date;
  date_add($financial_date, date_interval_create_from_date_string('2 months'));
  $quarter = ceil($financial_date->format('n')/3);

  return $financial_date->format('Y') . '-' . $quarter;
}

function format_FY($date)
{
  $financial_date = clone $date;
  date_add($financial_date, date_interval_create_from_date_string('2 months'));

  return $financial_date->format('Y');
}

function normalize(&$data)
{
  // Ensure fields added later, that are not present in all data, are available.
  if (!isset($data['total_image_product'])) {
    $data['total_image_product'] = [];
  }
}

function merge(&$data1, $data2)
{
  $data1['days'] += $data2['days'];
  $data1['total'] += $data2['total'];
  foreach ($data2['total_product'] as $product => $total) {
    if (empty($data1['total_product'][$product]))
      $data1['total_product'][$product] = 0;

    $data1['total_product'][$product] += $data2['total_product'][$product];
  }

  merge_product_plus_key($data1['unique_product'], $data2['unique_product']);
  merge_product_plus_key($data1['total_image_product'], $data2['total_image_product']);

  $data1['total_invalid'] += $data2['total_invalid'];
  $data1['bytes'] += $data2['bytes'];
}

function merge_product_plus_key(&$data1, $data2)
{
  foreach ($data2 as $product => $pairs) {
    if (empty($data1[$product]))
      $data1[$product] = [];

    foreach ($pairs as $key => $value) {
      if (empty($data1[$product][$key]))
        $data1[$product][$key] = 0;

      $data1[$product][$key] += $data2[$product][$key];
    }
  }
}

function summarize($data)
{
  static $products = [];

  $summary = [];

  $summary['-'] = [
    'total' => $data['total'],
    'total_invalid' => $data['total_invalid'],
    'bytes' => $data['bytes'],
    'unique' => 0,
  ];

  foreach ($data['total_product'] as $product => $total) {
    if (!product_filter($product)) continue;
    $summary_product = [
      'total' => $total,
    ];
    if (isset($data['unique_product'][$product])) {
      $unique_product = $data['unique_product'][$product];
      $summary_product += [
        'unique' => count($unique_product),
        'unqiue_average' => (float) (array_sum($unique_product) / count($unique_product)),
        'unqiue_max' => max($unique_product),
      ];
      // A UUID should be unique to a product, as such this should provide an
      // accurate count of total unique across all products.
      $summary['-']['unique'] += $summary_product['unique'];
    } else {
      $summary_product += [
        'unique' => 0,
        'unqiue_average' => (float) 0,
        'unqiue_max' => 0,
      ];
    }
    $summary[$product] = $summary_product;

    // Keep track of which products have been included in previous summary.
    if (!isset($products[$product])) $products[$product] = true;
  }

  // Fill empty data with zeros to achieve appropriate result in graph.
  $missing = array_diff(array_keys($products), array_keys($summary));
  foreach ($missing as $product) {
    $summary[$product] = [
      'total' => 0,
      'unique' => 0,
      'unqiue_average' => (float) 0,
      'unqiue_max' => 0,
    ];
  }

  return $summary;
}

function summarize_product_plus_key($data)
{
  static $keys = [];

  $summary = [];
  $products = array_merge(array_keys($keys), array_keys($data));
  foreach ($products as $product) {
    if (!product_filter($product)) continue;

    $keys_keys = isset($keys[$product]) ? array_keys($keys[$product]) : [];
    $data_keys = isset($data[$product]) ? array_keys($data[$product]) : [];
    $product_keys = array_merge($keys_keys, $data_keys);

    if (!isset($keys[$product])) $keys[$product] = [];
    $summary[$product] = [];
    foreach ($product_keys as $key) {
      // Fill empty data with zeros to achieve appropriate result in graph.
      $keys[$product][$key] = true;
      $summary[$product][$key] = isset($data[$product][$key]) ? $data[$product][$key] : 0;
    }
  }

  return $summary;
}

function product_filter($product)
{
  return (bool) preg_match(PRODUCT_PATTERN, $product);
}

function date_period_reversed($begin, $interval, $end)
{
  $interval = DateInterval::createFromDateString($interval);
  $period = new DatePeriod($begin, $interval, $end);
  return array_reverse(iterator_to_array($period));
}

function write_summary($interval, DateTime $value, $summary, $tags = [], $prefix = 'access')
{
  $measurement = $prefix . '_' . $interval;
  $points = [];
  foreach ($summary as $product => $fields) {
    $points[] = new Point($measurement, null,
      ['product' => $product] + $tags, $fields, $value->getTimestamp());
  }
  write($points);
  return count($points);
}

function write_summary_product_plus_key($interval, DateTime $date, $summary, $prefix)
{
  $measurement = $prefix . '_' . $interval;
  $points = [];
  foreach ($summary as $product => $pairs) {
    foreach ($pairs as $key => $value) {
      $points[] = new Point($measurement, null,
        ['product' => $product, 'key' => $key], ['value' => $value], $date->getTimestamp());
    }
  }
  write($points);
  return count($points);
}

function write($points)
{
  static $database = null;

  if (!$database) {
    $database = InfluxDB\Client::fromDSN('influxdb://0.0.0.0:8086/osrt_access');
  }

  if (!$database->writePoints($points, Database::PRECISION_SECONDS)) die('failed to write points');
}
