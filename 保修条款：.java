保修条款：
保修期应以购买凭证上的购买时间加上5年为准，如果最终用户没有开具或保留有效购买凭证，我们会以原始序列号的保修期为准。


function warrantyPeriod($purchaseDate, $serialNumber) {
  $purchaseDate = strtotime($purchaseDate);
  $warrantyPeriod = strtotime('+5 years', $purchaseDate);
  $now = time();
  if ($now > $warrantyPeriod) {
    return 'Out of warranty';
  } else {
    return date('Y-m-d', $warrantyPeriod);
  }
}