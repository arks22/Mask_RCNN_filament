# 佐々木実験1

細長いアンカースケールを追加

## Configurations(hyper parameters)

実験2と同様に、小さいアンカースケールを削除

| Parameter              |Value                   |
|------------------------|------------------------|
|**RPN_ANCHOR_RATIOS**   |[0.25, 0.5, 1, 2, 4]    |
|**RPN_ANCHOR_SCALES**   |(128, 256, 512)         |
|**RPN_ANCHOR_STRIDE**   |1                       |

## Dataset
ピクセル面積600以下のフィラメントを教師データから除外

| Task         | Year             |
|--------------|------------------|
| train        | 2012             |
| validation   | any(defaul=2013) |
| test         | any              |
